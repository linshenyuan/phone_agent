"""主循环 —— 截图、问模型、执行动作，直到完成或达到步数上限。"""

from __future__ import annotations

import json
import time
from typing import Any

from .actions import (StuckDetector, execute_action, is_blank_frame,
                      report_need_human, screen_fingerprint)
from .adb import AdbError, screenshot
from .apps import current_package, detect_app_in_task, ensure_yadb, launch_app, reset_to_home
from .config import (APP_ALIASES_FILE, BLANK_FRAME_MAX, TMP_DIR, EXIT_NEED_HUMAN,
                     MAX_CONSECUTIVE_TYPE, MAX_SLIDE_TOTAL, OPEN_FAIL_STREAK_MAX)
from .output import info, prune_tmp, redact_text
from .tasks import (_is_pure_open_task, _is_trivial_task, _looks_like_text_task,
                    _password_prompt_hit, _task_may_need_password)
from .ui import point_hits_editable, read_focused_text, visible_texts
from .vision import ask_model, parse_point, resize_for_model
from .deps import OpenAI



# ============================================================
# 主循环
# ============================================================

def _adb_fail(step: str, exc: AdbError) -> int:
    """
    adb 类异常的统一收尾 —— **必须**留下 `[结论]` 行。

    ★ 为什么需要（2026-10-05，Claude 审查 B2）：`ensure_yadb` / `reset_to_home` /
      `screenshot` 原本都没被包住 —— 拔线或 adb 掉线时直接抛 traceback，
      调用方（尤其是 LLM）拿不到它赖以判断的 `[结论]` 行，只能自己瞎解释，
      甚至编一个与事实无关的原因回给用户（这个坑 2026-09-30 已经吃过一次）。
    """
    print(f"\n{'=' * 56}")
    print(f"[错误] {step} 失败：{exc}")
    print("[结论] 任务【未完成】—— 退出码 1 = adb 出错（多半是掉线 / 拔线），"
          "**不是模型判断失误**")
    print("=" * 56)
    return 1


def _conclude(exit_code: int, headline: str, hint: str = "") -> int:
    """
    退出前的统一收尾 —— **必须**打出 `[结论]` 行。

    ★ 为什么（2026-10-05，mimo 审查 P1-6）：调用方（尤其是 LLM）往往**只读尾部**；
      缺了 `[结论]` 行，它会照着退出码自己编原因 —— 2026-09-30 已经吃过一次，
      一次**成功**的任务被说成失败，还编了个「该文件夹不存在」的假理由。
      所以每条退出路径都要把「是什么 / 不是什么」写清楚。
    """
    print(f"\n{'=' * 56}")
    print(headline)
    if hint:
        print(f"       说明：{hint}")
    print(f"[结论] 任务【未完成】—— 退出码 {exit_code}")
    print("=" * 56)
    return exit_code


def _password_guard_reason(task: str, device: str | None) -> str:
    """
    这次 TYPE 是不是「可能在输密码」。返回原因文字；不涉及则返回空串。

    ★ 两层判据（2026-10-05，Claude B5 / mimo P1-3）：
      甲：任务文本里明确写了密码/验证码 —— 零成本，误判率低
      乙：屏幕上出现「请输入密码」这类提示 —— 需要一次 dump（约 1.7 秒）

    ★ 为什么不直接「识别密码框」：WebView 的 `<input type="password">` 和
      Flutter 的 `obscureText` 输入框在 `uiautomator dump` 里**根本不存在**
      （没有 EditText 节点、也不带 password 属性）。而密码一旦被代输，
      就是**真的交给了 App** —— 只能靠这两层间接判据提前拦下。
    """
    if _task_may_need_password(task):
        return f"任务里提到了密码/验证码（「{task}」）—— 脚本不代输密码"
    hit = _password_prompt_hit(visible_texts(device))
    if hit:
        return f"当前屏幕上出现「{hit}」—— 很可能正在要求输入密码/验证码"
    return ""


def _for_log(action: dict[str, Any]) -> dict[str, Any]:
    """打日志用的动作副本：TYPE 的正文脱敏（日志会落盘，不该记明文）。"""
    if str(action.get("action_type", "")).upper() != "TYPE":
        return action
    out = dict(action)
    out["value"] = redact_text(out.get("value", ""))
    return out


def run(task: str, device: str | None, client: OpenAI, model: str,
        view_width: int, max_steps: int, step_delay: float,
        dry_run: bool = False, reset_app: str | None = None,
        no_reset: bool = False, auto_launch: bool = True) -> int:
    """
    跑完一个任务。

    :return: 进程退出码，0 表示任务完成，1 表示未完成或出错
    """
    # ★ dry-run 必须**完全不碰手机**（2026-10-05，mimo 审查 P1-2）：
    #   旧实现把 ensure_yadb 放在这个判断**之前**，它会真的往手机 push yadb ——
    #   于是 README 里吹的「确认闸门」并不干净。现在 dry-run 直接跳过。
    if dry_run:
        info("[dry-run] 只做决策，不会真的操作手机\n")
        info("[dry-run] 跳过 yadb 部署（不往手机 push 任何文件）")
        has_yadb = False
    else:
        try:
            has_yadb = ensure_yadb(device, verbose=True)
        except AdbError as exc:
            return _adb_fail("连接设备（ensure_yadb）", exc)

    # ★ 执行前复位：把手机推回桌面，保证起点可复现。
    #   不做的话，起点就是上一轮的终点 —— 实测导致过 1 步假成功。
    if no_reset:
        info("[跳过复位] --no-reset 已指定，将沿用当前屏幕作为起点")
    elif dry_run:
        info("[dry-run] 跳过复位（不操作手机）")
    else:
        try:
            reset_to_home(device, kill_package=reset_app)
        except AdbError as exc:
            return _adb_fail("复位到桌面（reset_to_home）", exc)

    # 起点基线：记下开始时的前台包名，完成时用来判断「到底有没有离开过起点」
    try:
        baseline_pkg = current_package(device)
    except AdbError as exc:
        return _adb_fail("读取当前前台应用", exc)
    info(f"[起点] 前台应用：{baseline_pkg or '未知'}")

    # ★ 方案A：任务里点名了某个 App 时，直接用 adb 把它拉起来，**绕开桌面**。
    #   为什么必须做（2026-09-30 实测）：vivo 桌面（com.bbk.launcher2）是自绘界面，
    #   `uiautomator dump` 只拿到 45 个节点、只剩一个搜索框 —— 图标根本读不出来，
    #   靠截图猜图标坐标基本必错。「在桌面上找图标再点」在本机是死路。
    #   注意：--no-reset 时跳过（用户明确要求沿用当前屏幕，不该被顶掉）。
    if auto_launch and not dry_run and not no_reset:
        target = detect_app_in_task(task, device)
        # ★ 只有「纯打开」任务才自动启动（2026-10-05 修 detect_app_in_task 误启动）：
        #   旧实现把 detect_app_in_task 的**模糊猜测**直接拿去启动 ——
        #   任务「打开微信读书」里含别名「微信」→ 被猜成微信 → 启动了**错误的 App**。
        #   现在要求：任务剥掉「打开」类动词 + 这个 App 的别名后**什么都不剩**
        #   （即纯打开），才允许自动启动；否则交给模型自己 OPEN（提示词已要求）。
        if target and _is_pure_open_task(task, target):
            try:
                launch_app(target, device)
                time.sleep(1.2)          # 等首屏渲染出来，避免截到启动画面
                fg = current_package(device)
                info(f"[自动启动] 纯打开任务，已用 adb 拉起：{target}"
                     f"（当前前台：{fg}）")

                # ★ 纯打开类任务：App 已在前台就直接收工，不烧模型（2026-10-01 加）
                #   为什么需要：实测「打开轻小说文库」时 auto_launch 已经拉起 App，
                #   但模型看到界面后不确定任务算不算完成，又乱点了 3 次才罢休。
                #   护栏用 _is_pure_open_task 而不是 _is_trivial_task ——
                #   后者用「长度 ≤ 8」当判据，「打开微信发消息」(7 字) 会被误判成
                #   纯打开任务，导致消息没发就宣布完成。
                if fg == target:
                    print(f"\n{'=' * 56}")
                    print("任务完成（App 已启动；纯打开类任务，无需模型决策）")
                    print("=" * 56)
                    print("[结论] 任务【已完成】—— 退出码 0")
                    return 0
            except AdbError as exc:
                info(f"[自动启动] 跳过：{exc}")
        else:
            info("[自动启动] 不是「纯打开」任务或没识别出明确 App，从当前屏幕起步")

    history: list[dict[str, Any]] = []
    finished_but_suspect = False
    # ★ 连续 OPEN 失败计数（2026-10-05 加）：到 OPEN_FAIL_STREAK_MAX 就判「不支持的应用」收工
    open_fail_streak = 0
    # ★ 连续黑屏步数（2026-10-05 加）：到 BLANK_FRAME_MAX 就停下问人工
    blank_streak = 0

    for step in range(1, max_steps + 1):
        try:
            real_img = screenshot(device)
        except AdbError as exc:
            return _adb_fail(f"第 {step} 步截图", exc)
        view_img = resize_for_model(real_img, view_width)

        # 卡死检测器按真实屏幕尺寸构造，容差用像素算、比较用归一化坐标
        if step == 1:
            stuck = StuckDetector(real_img.width, real_img.height)

        # ★ 界面粗指纹（2026-10-05，Claude 审查 10.1）：给卡死检测用 ——
        #   判断「点不动」时界面到底有没有变。计算器连按数字 / 步进器连点 /
        #   翻页连点都会让界面变，不该被判成卡死。
        screen_fp = screen_fingerprint(real_img)

        # ★ 黑屏检测（2026-10-05，Claude 审查 阅读6）：锁屏 / FLAG_SECURE / 息屏时
        #   截图是纯黑的，模型看不见任何东西却照样「决策」= 盲操作。
        #   连续 BLANK_FRAME_MAX 步全黑就停下问人工（**不自动解锁** —— 解锁要密码）。
        if not dry_run and is_blank_frame(real_img):
            blank_streak += 1
            info(f"[黑屏] 第 {step} 步截图几乎全黑（连续 {blank_streak} 次）")
            if blank_streak >= BLANK_FRAME_MAX:
                report_need_human(
                    device, real_img, step,
                    "截图连续多次几乎全黑 —— 手机可能已锁屏 / 息屏，"
                    "或当前应用禁止截屏（FLAG_SECURE）",
                    headline="[屏幕全黑，请人工接手]",
                    conclusion=(
                        f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                        f"屏幕全黑，**不是脚本失败**\n"
                        f"       请解锁手机、或换到可截屏的界面后重新发起任务。"))
                return EXIT_NEED_HUMAN
        else:
            blank_streak = 0

        info(f"\n{'=' * 56}")
        info(f"第 {step}/{max_steps} 步   屏幕 {real_img.width}x{real_img.height}"
             f"   喂给模型 {view_img.width}x{view_img.height}")
        info("=" * 56)

        try:
            action = ask_model(client, model, view_img, task, history)
        except Exception as exc:
            return _conclude(1, f"[错误] 模型调用失败：{exc}",
                             "模型服务无响应或报错，**不是任务本身失败**")

        # ★ 日志脱敏（2026-10-05，Claude 审查 B4）：TYPE 的正文可能是密码/私密消息，
        #   而日志会落盘长期保留。注意 history 里仍存**原文**（模型要看到自己输过什么）。
        info(f"模型决策：{json.dumps(_for_log(action), ensure_ascii=False)}")

        # ★ 连续 TYPE 保护的新判据（2026-10-04 改）：只有「上一步真的点在输入框上」
        #   才清零计数。判断方式 = 读界面树看这个坐标有没有压住 EditText，
        #   取代旧的「看 y 是否在屏幕下方」猜测（换机型 / 横屏 / 浮窗会猜错，
        #   底部普通按钮也会被误当成重新聚焦输入区而绕过保护）。
        #   仅当「已有计数待清」时才去读界面，平时不读 —— 避免每步多花 ~1.7s。
        click_on_input = None
        click_checked = False
        if (str(action.get("action_type", "")).upper() in ("CLICK", "LONGPRESS")
                and stuck.consecutive_type > 0 and not dry_run
                and action.get("point") is not None):
            click_checked = True
            nx, ny = parse_point(action["point"])
            real_pt = (int(nx / 1000 * real_img.width),
                       int(ny / 1000 * real_img.height))
            click_on_input = point_hits_editable(device, real_pt)

        # 卡死检测：命中就立刻收工，不再把剩下的步数烧光。
        # 因为 agent 一旦卡住，后续动作全是重复的，继续跑只会污染现场。
        stuck_reason = stuck.update(action,
                                    click_on_input=click_on_input,
                                    click_checked=click_checked,
                                    screen_fp=screen_fp)
        if stuck_reason:
            return _conclude(
                2, f"[卡死] 第 {step} 步判定为原地打转，提前终止",
                f"原因：{stuck_reason}；脚本**主动终止**，不是模型判断失误")

        # 拦截：不终止任务，只跳过这一步，让模型换别的手段。
        #   * 连续 TYPE —— 模型点不中发送按钮时会反复追加文字，退化成 200 字乱码
        #   * 滑动超限 —— 目标明明已经可见，它还在无意义地滑（2026-10-01 加）
        block = stuck.should_block(action)
        # ★ 拦之前先回读输入框（2026-10-05，Claude 审查 10.3）：框是**空的** ->
        #   说明上一句已经发出去了（或上次输入本来就没上屏），这是合法的「再打一句」。
        #   旧实现会在这里无谓拦下，提示还错说「输入框里已有内容」。
        #   读不到（None）时保守起见**仍然拦**。
        if (block
                and str(action.get("action_type", "")).upper() == "TYPE"
                and not dry_run and read_focused_text(device) == ""):
            info("[放行] 回读到输入框是空的 —— 上一句应该已经发出去了，允许再输入")
            stuck.reset_type_guard("回读到输入框为空 -> 视为已发送，放行一次 TYPE")
            block = False

        if block:
            # ★ 判断不出输入框、且模型仍要「连打第二次」-> 停下报告，交给人工（2026-10-04 加）
            #   为什么不再默默跳过：判断不出通常是界面读不透（Flutter/WebView），
            #   这时小模型极易反复 TYPE 把文字叠成乱码，停下来让人接手更安全。
            if stuck.escalate_stop:
                reason = ("模型想连续输入第二次，但当前界面读不透"
                          "（看不到输入框，多为 Flutter/WebView 界面）—— "
                          "小模型在这里无法可靠判断，请人工接手")
                report_need_human(
                    device, real_img, step, reason,
                    headline="[模型无法判断输入框，请人工接手]",
                    conclusion=(
                        f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                        f"模型无法判断输入框，**不是脚本失败**\n"
                        f"       脚本已停在原处，可人工完成后重新发起任务。"))
                return EXIT_NEED_HUMAN
            if action.get("action_type") == "SLIDE":
                note = (f"[已拦截] 滑动次数已用尽（{stuck.slide_total} 次，上限 "
                        f"{MAX_SLIDE_TOTAL}），这一步没有执行。"
                        f"目标元素应该已经可见了 —— 请直接 CLICK 点它；"
                        f"如果确认任务已经完成，直接输出 COMPLETE。不要再用 SLIDE 试探。")
                info(note)
                info(f"[拦截依据] 滑动总次数={stuck.slide_total}（上限 {MAX_SLIDE_TOTAL}）"
                     f"  屏幕={real_img.width}x{real_img.height}")
                history.append({"action_type": "BLOCKED_SLIDE", "value": ""})
            else:
                note = (f"[已拦截] 这一步 TYPE「{redact_text(action.get('value', ''))}」未执行："
                        f"输入框里已有内容，再输入会连成一串。"
                        f"请改为点击发送按钮 —— 自己看截图找，"
                        f"它是负责把输入框内容发出去的那个控件，通常紧挨着输入框。")
                info(note)
                # ★ 为什么打印这行（2026-09-29 加）：上一轮真机日志里「第 5 步 TYPE 被拦」，
                #   但用同一动作序列做单测却不拦 —— 光看日志无法区分是哪个计数器在起作用。
                #   把计数、窗口内容和屏幕尺寸都打出来，下次出问题一眼就能定位。
                info(f"[拦截依据] 连续 TYPE 计数={stuck.consecutive_type}"
                     f"（本次算上则 {stuck.consecutive_type + 1}，上限 {MAX_CONSECUTIVE_TYPE}）"
                     f"  屏幕={real_img.width}x{real_img.height}"
                     f"  上次点击：{stuck.last_reset_note or '（还没点过）'}")
                history.append({"action_type": "BLOCKED_TYPE",
                                "value": str(action.get("value", ""))})
            time.sleep(step_delay)
            continue

        # ★ SLIDE 前先 dump 看界面文字（2026-10-01 加）
        #   目的：让模型「睁眼看清楚」当前界面有什么 —— 它只看截图时认不出
        #   「我的收藏」这类语义，会在目标已可见时还反复滑动。
        #   文字随 action 一起进 history，下一步模型就能看到。
        if action.get("action_type") == "SLIDE" and not dry_run:
            texts = visible_texts(device)
            if texts:
                action["screen_text"] = " / ".join(texts)
                info(f"[SLIDE 前查屏] {action['screen_text']}")

        # 把模型最近一次点击的坐标传下去：TYPE 时用它判断「想往哪个输入框打字」。
        # ★★ 2026-10-04：只有「上一步动作本身就是 CLICK」时才传坐标。
        #   否则（模型点完又滑动/返回/等待才 TYPE）坐标是旧的，落在别的框里会误聚焦。
        last_click = stuck.clicks[-1] if stuck.clicks else None
        prev_action = history[-1] if history else None
        prefer_point = (last_click
                        if prev_action is not None
                        and str(prev_action.get("action_type", "")).upper() == "CLICK"
                        else None)
        # ★ 密码防护（2026-10-05）：任何 TYPE 之前先确认「这不是在输密码」。
        #   挡不住的话，密码会被真的输进 App，而且 Flutter 场景还会被判成成功。
        if not dry_run and str(action.get("action_type", "")).upper() == "TYPE":
            why = _password_guard_reason(task, device)
            if why:
                report_need_human(
                    device, real_img, step, why,
                    headline="[涉及密码，请人工输入]",
                    conclusion=(
                        f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                        f"需要人工输入密码/验证码，**不是脚本失败**\n"
                        f"       脚本已停在原处，请人工输完后再重新发起任务。"))
                return EXIT_NEED_HUMAN

        try:
            res = execute_action(action, real_img.size, device, has_yadb, dry_run,
                                 prefer_point=prefer_point)
        except AdbError as exc:
            return _conclude(1, f"[错误] 执行失败：{exc}",
                             "adb 出错（多半是掉线 / 拔线），**不是模型判断失误**")

        info(f"执行结果：{res.note}")
        # ★ 失败也要进历史（2026-10-05 改）：旧写法无论成败都塞原始动作，
        #   模型看不到失败就会原样重试（OPEN 尤其致命：会无限 OPEN 同一个不存在的 App）。
        if res.ok:
            history.append(action)
        else:
            failed = dict(action)
            failed["failed"] = True
            failed["error"] = res.note
            history.append(failed)

        # ★ 需要人工介入：立即停止，不再尝试（2026-10-01 加）
        if res.need_human:
            report_need_human(device, real_img, step, res.human_reason)
            return EXIT_NEED_HUMAN

        if not res.ok:
            print("[警告] 动作无法执行，继续下一步尝试别的策略")
            # 输入彻底失败（重试也没上屏）是硬伤，继续跑必然是空转，直接收工
            if action.get("action_type") == "TYPE":
                return _conclude(
                    2, "[终止] 文字输入无法完成，后续步骤只会重复尝试，提前结束",
                    "输入反复没上屏（可能是输入法或界面问题）")
            # ★ 连续 OPEN 失败到上限 -> 停下问用户是哪个应用（#3，2026-10-05）
            if str(action.get("action_type", "")).upper() == "OPEN":
                open_fail_streak += 1
                if open_fail_streak >= OPEN_FAIL_STREAK_MAX:
                    app_name = str(action.get("app", "")).strip()
                    reason = (f"模型想打开「{app_name}」，但它不在应用白名单里，"
                              f"无法启动 —— 请人工确认这是哪个 App")
                    report_need_human(
                        device, real_img, step, reason,
                        headline="[模型无法识别该应用，请人工接手]",
                        conclusion=(
                            f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                            f"不认识的应用，**不是脚本失败**\n"
                            f"       解决办法：把「名称 = 包名」加进 {APP_ALIASES_FILE}，"
                            f"再重新发起任务。"))
                    return EXIT_NEED_HUMAN
        else:
            open_fail_streak = 0
        if res.finished:
            print(f"\n{'=' * 56}")
            print(f"任务完成（用了 {step} 步）")
            if res.result_text:
                print(f"结果：{res.result_text}")

            # ---- 可疑完成检查 ----
            # 实测踩坑（2026-09-29）：模型会在**什么都没做**的情况下喊 COMPLETE，
            # 退出码还是 0，调用方会以为成功了。这类「假成功」比空转更危险。
            suspects: list[str] = []

            # ★ 被拦的 TYPE **不算**「打过字」（2026-10-05，mimo 审查 P2）：
            #   它根本没执行，算进去会掩盖「全程没打字却喊完成」的假成功。
            did_type = any(h.get("action_type") == "TYPE" for h in history)
            if not did_type and _looks_like_text_task(task):
                suspects.append("任务要求发文字，但整个过程一次 TYPE 都没执行过")

            # 步数太少：正常的多步任务不可能 1~2 步就完成。
            # 实测第 2 轮就是「1 步 COMPLETE」—— 起点停在 QQ 聊天界面导致的。
            if step <= 2 and not _is_trivial_task(task):
                suspects.append(f"只用了 {step} 步就宣告完成，对多步任务来说不合常理")

            # 前台应用和起点一模一样：说明压根没离开过起点。
            # 这是「起点状态污染」最直接的证据。
            end_pkg = current_package(device)
            if baseline_pkg and end_pkg and end_pkg == baseline_pkg \
                    and _looks_like_text_task(task):
                suspects.append(f"结束时前台仍是起点的「{end_pkg}」，全程没离开过起点")

            if suspects:
                print()
                print("[警告] 可疑完成，请人工核对屏幕：")
                for s in suspects:
                    print(f"       - {s}")
                finished_but_suspect = True

            # 把完成时的屏幕存下来当证据（不带这个，事后没法判断是不是假成功）
            # ★ 必须基于脚本目录（2026-09-30 审计）：旧写法 Path("tmp") 是相对路径，
            #   取决于调用方 cwd —— 实测 Pi 从 <用户目录> 调用时，
            #   截图被写进了 <用户目录>\tmp\（真实遗留了 4 张）。
            # ★ 保存失败不能静默：这条截图是判断「假成功」的唯一依据，
            #   连它都拿不到的话，事后完全无法复核。
            try:
                shot_path = TMP_DIR / f"complete_step{step}.png"
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                real_img.save(shot_path)
                info(f"完成时截图已存：{shot_path}")
                prune_tmp()          # 只留最近 TMP_KEEP 张，防无限累积
            except Exception as exc:
                print(f"[警告] 完成截图保存失败（{exc}）—— 事后将无法核对是否假成功")

            print("=" * 56)
            # ★ 最后几行必须写清「成功还是失败」—— 调用方（尤其是 LLM）往往只读尾部。
            #   实测踩坑（2026-09-30，代价很大）：Pi 看到 `Command exited with code 3`
            #   就当成失败，还编了个「该文件夹不存在」的理由回给用户 ——
            #   而那次任务其实**已经成功打开了目标 App**。
            #   退出码 3 的含义是「已宣告完成 + 有可疑点需人工核对」，**不是失败**。
            if finished_but_suspect:
                print("[结论] 任务【已完成】—— 退出码 3 = 完成但有可疑点，**不是失败**")
                print("       上面的警告只是提醒人工核对，不要据此回复「任务失败」。")
            else:
                print("[结论] 任务【已完成】—— 退出码 0")
            return 3 if finished_but_suspect else 0

        time.sleep(step_delay)

    return _conclude(1, f"[结束] 达到最大步数 {max_steps} 仍未完成",
                     "步数耗尽，任务没做完（可加大 --max-steps，或把任务拆小）")
