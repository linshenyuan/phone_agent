"""动作执行 —— 把模型给的动作翻译成 adb 命令，并做卡死检测。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .adb import AdbError, adb, keyevent, swipe, tap
from .apps import current_package, input_text, launch_app
from .config import TMP_DIR, BLANK_FRAME_LEVEL, BLANK_FRAME_RATIO, DEFAULT_LONGPRESS_DURATION, DEFAULT_SLIDE_DURATION, EXIT_NEED_HUMAN, MAX_CONSECUTIVE_TYPE, MAX_SLIDE_TOTAL, SCREEN_FP_SIZE, SCREEN_FP_STATUS_BAR, STUCK_POSITION_TOLERANCE_PX, STUCK_REPEAT_THRESHOLD, STUCK_SLIDE_REPEAT_THRESHOLD, STUCK_TYPE_REPEAT_THRESHOLD, STUCK_WINDOW, TYPE_CLEAR_MAX, TYPE_RETRY, TYPE_VERIFY_DELAY, WAIT_SECONDS_DEFAULT, WAIT_SECONDS_MAX
from .output import info, prune_tmp, redact_text
from .ui import focus_editable_box, read_focused_text
from .vision import InvalidActionError, parse_point
from .deps import Image

def _norm(s: Any) -> str:
    """
    去掉所有空白 —— 用于比较「输入框里读到的字」和「我们想输入的字」。

    必要性（2026-10-05）：手机号 / 金额 / 日期类输入框会自动插空格
    （输入 `13812345678`，框里读到 `138 1234 5678`），直接比会误判失败。
    """
    return re.sub(r"\s+", "", str(s or ""))


@dataclass
class ExecResult:
    """一次动作执行的结果。"""

    ok: bool
    note: str = ""
    finished: bool = False
    result_text: str = ""
    # ★ 需要人工介入（2026-10-01 加）：命中就**立即停止任务**，不是继续尝试。
    #   目前触发场景：模型要往密码框里打字 / 界面有多个输入框无法确定目标。
    need_human: bool = False
    human_reason: str = ""
    # ★ 交给 report_need_human 的抬头与结论（2026-10-05）：
    #   留空则用 report_need_human 的默认措辞（默认是「需要人工输入（密码）」）。
    #   为什么要有它：多输入框歧义时若沿用默认措辞，脚本会打出**与事实相反**的
    #   「需要人工输入（密码）」—— 而真实原因只是「有多个输入框」。
    human_headline: str = ""
    human_conclusion: str = ""




# ============================================================
# 卡死检测
# ============================================================

def _norm_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两个 0-1000 归一化坐标之间的欧氏距离。"""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5




def screen_fingerprint(img: Image.Image) -> int:
    """
    截图的**粗指纹**（8×8 灰度均值哈希），用来判断「界面到底有没有变化」。

    ★ 为什么要它（2026-10-05）：旧判据只看「是不是打在同一个点」，
      于是计算器连按数字、步进器连点加减、「下一页」连点都会被判成卡死并终止任务。
      加上「界面没变」这一条：变了 = 有进展，不算卡死。

    ★ 分辨率是**可调**的（`SCREEN_FP_SIZE`，默认 16×16）：先裁掉顶部状态栏
      （时钟一直在跳），再缩到该尺寸。调大更灵敏、调小更抗动画干扰。
    """
    w, h = img.size
    top = int(h * SCREEN_FP_STATUS_BAR)
    small = img.crop((0, top, w, h)).convert("L").resize((SCREEN_FP_SIZE, SCREEN_FP_SIZE))
    # L 模式每像素 1 字节，tobytes() 直接拿到灰度值序列（比 getdata() 快，
    # 且避开 Pillow 的 getdata 弃用警告）。
    px = small.tobytes()
    avg = sum(px) / len(px)
    bits = 0
    for i, v in enumerate(px):
        if v >= avg:
            bits |= 1 << i
    # ★ 再拼一个**粗亮度**分量（32 档）：纯均值哈希对「均匀图」是退化的 ——
    #   全黑和全白的结构比特完全相同（都是全 1），于是「黑屏 -> 白屏」会被判成
    #   「界面没变」。补上亮度档位才能区分这种纯色切换（2026-10-05 实测发现）。
    #   ⚠️ 偏移量必须跟着 SCREEN_FP_SIZE 走（结构位占 SIZE² 位）—— 写死 64 的话，
    #      调大分辨率后亮度会**撞进结构位**，指纹直接错乱。
    return bits | ((int(avg) >> 3) << (SCREEN_FP_SIZE * SCREEN_FP_SIZE))


def is_blank_frame(img: Image.Image) -> bool:
    """
    整屏是不是**近黑**（锁屏 / FLAG_SECURE / 息屏的典型表现）。

    ★ 为什么要它（2026-10-05）：这些情况下截图是纯黑的，
      模型看不见任何东西却照样「决策」= 盲操作，结果完全不可预期。
      检出后由调用方**停下问人工**（不自动解锁 —— 解锁要密码，不该代做）。
    """
    small = img.convert("L").resize((64, 64))
    px = small.tobytes()                    # L 模式：每像素 1 字节
    dark = sum(1 for v in px if v < BLANK_FRAME_LEVEL)
    return dark >= BLANK_FRAME_RATIO * len(px)


class StuckDetector:
    """
    判断 agent 是不是在原地打转，并说明理由。

    为什么需要它（实测踩坑）：yadb 输入中文时会**静默失败** ——
    返回 {"code":0,"message":"Text input sent"} 但输入框其实是空的。
    模型看不到任何错误，于是一遍遍重试同样的动作，25 步全部烧光。

    旧实现的三个缺陷：
      1. 用 `len(set(recent_clicks[-3:])) == 1` 要求坐标**完全相等**，
         而模型有 ±1 抖动 -> 判据永远为假，实际上从未触发过；
      2. `else: recent_clicks.clear()` —— 一个 TYPE 就把窗口清空，
         而「点输入框 -> 输字 -> 点输入框 -> 输字」正是最典型的空转形态；
      3. 只 print 警告，不做任何干预 -> 该空转还是空转。

    这一版：按**归一化距离容差**归并，三类动作各自独立计数，互不清零；
    命中就由调用方终止任务。

    ★ 容差怎么换算（2026-10-05 澄清）：
      归一化容差 = STUCK_POSITION_TOLERANCE_PX / **屏幕长边像素** × 1000，
      **横竖用同一个归一化数**。所以在 1080×2408 上：
        纵向 ≈ 30px；横向 ≈ 13px（同一归一化值乘横向像素密度）
      —— 旧注释笼统写「换算成像素后约 30px」，**只对纵向成立**，不准。
    """

    def __init__(self, real_w: int, real_h: int) -> None:
        # 归一化坐标的一个单位 ≈ 多少像素。用长边算，宁可宽松也不要误判。
        self._px_per_unit = max(real_w, real_h) / 1000.0
        self.clicks: list[tuple[float, float]] = []
        # ★ 每次点击时界面的粗指纹（与 self.clicks 一一对应，2026-10-05 加）：
        #   判断「点不动」时要看界面有没有变，否则计算器/步进器会被误判成卡死。
        self._click_fps: list[int | None] = []
        self.types: list[str] = []
        self.slides: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self.hits: list[str] = []
        # 自上次「点输入框 / 点发送按钮」以来，连续 TYPE 了几次
        self.consecutive_type = 0
        # ★ 整局滑动总次数（2026-10-01 加）：超过 MAX_SLIDE_TOTAL 就拦截。
        #   与 slides 窗口的区别：那个判「重复滑同一条」，这个判「滑得太多」。
        self.slide_total = 0
        # 最近一次点击对「连续 TYPE 计数」的影响，仅用于日志排查
        self.last_reset_note = ""
        # ★ 最近一次点击「是不是输入框」判断不出来（界面读不透）—— 见 update()
        self._unknown_click = False
        # ★ should_block() 判定要拦 TYPE、且原因正是上面这个「看不清」时置 True，
        #   让调用方**停下并报告**，而不是默默跳过（2026-10-04 加）
        self.escalate_stop = False

    # -- 判定辅助 --------------------------------------------------

    def _same_spot(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """两个归一化坐标是否落在同一个点的容差圈内。"""
        tol = STUCK_POSITION_TOLERANCE_PX / self._px_per_unit
        return _norm_dist(a, b) <= tol

    def _count_stuck_clicks(self) -> int:
        """
        统计窗口里「**打在同一个点、且界面没变**」的最大次数。

        ★ 为什么要加「界面没变」（2026-10-05）：
          只看「是不是同一个点」，会把**计算器连按数字、步进器连点加减、
          翻页按钮连点**都误判成卡死并终止任务。界面变了 = 有进展，不算卡死。
        ★ 指纹未知（None，调用方没给）时**退回旧判据**（只看点），保持向后兼容。

        ⚠️ 它（以及旧版那个只数「众数」的 `_count_same_spot`）**识别不了
          「两个点来回跳」**（A B A B）：窗口只有 STUCK_WINDOW=4，这种模式下最多
          计数到 2，够不到阈值 3。真机日志里没出现过这种卡死，暂不为此改逻辑 ——
          因为「两点来回跳就判卡死」会误杀正常操作（反复开关同一设置项、点输入框→点发送）。
          （`_count_same_spot` 已无生产调用，2026-10-05 清理删除，其说明并入此处。）
        """
        best = 0
        pts, fps = self.clicks, self._click_fps
        for i, p in enumerate(pts):
            fi = fps[i] if i < len(fps) else None
            n = 0
            for j, q in enumerate(pts):
                if not self._same_spot(p, q):
                    continue
                fj = fps[j] if j < len(fps) else None
                if fi is None or fj is None or fi == fj:
                    n += 1
            best = max(best, n)
        return best

    def reset_type_guard(self, note: str) -> None:
        """
        放行一次「连续 TYPE」：清零计数并记下原因。

        ★ 用途（2026-10-05）：拦之前回读到输入框是**空的** ——
          说明上一句已经发出去了（或上次输入本来就没上屏），
          这是合法的「再打一句」，不该被连续 TYPE 保护拦下。
        """
        self.consecutive_type = 0
        self._unknown_click = False
        self.last_reset_note = note

    # -- 对外接口 --------------------------------------------------

    def update(self, action: dict[str, Any],
               click_on_input: bool | None = None,
               click_checked: bool = False,
               screen_fp: int | None = None) -> str | None:
        """
        喂入一个动作，返回卡死原因；没卡死返回 None。

        :param click_on_input: CLICK/LONGPRESS 时，这次点击是否压在输入框上
                               （True / False / None=看不清），由调用方读界面树判断。
        :param click_checked:  调用方这次**有没有去判断**（False 表示没查，
                               通常因为当前没有待清的连续 TYPE 计数）。
        :param screen_fp:      这次动作**执行前**的界面粗指纹（`screen_fingerprint()`）。
                               传了就要求「同点连点 **且界面没变**」才算卡死 ——
                               否则计算器连按数字、步进器连点会被误判（2026-10-05）。
                               不传则退回旧判据（只看点）。
        :return: 人类可读的卡死描述，命中后调用方应立即终止任务
        """
        name = str(action.get("action_type", "")).upper()

        if name in ("CLICK", "LONGPRESS"):
            point = action.get("point")
            if point is not None:
                p = parse_point(point)
                # ★ 清零判据（2026-10-04 改）：只有「这次点击真的压在输入框上」才清零，
                #   允许模型输入下一句。
                #   旧版看「y 是否在屏幕下方」，换机型 / 横屏 / 浮窗会猜错，
                #   底部普通按钮也会被误当成「重新聚焦输入区」从而绕过连续 TYPE 保护。
                if click_checked:
                    if click_on_input is True:
                        self.consecutive_type = 0
                        self._unknown_click = False
                        self.last_reset_note = (
                            f"CLICK ({p[0]:.0f},{p[1]:.0f}) 命中输入框 -> 计数清零")
                    elif click_on_input is False:
                        self._unknown_click = False
                        self.last_reset_note = (
                            f"CLICK ({p[0]:.0f},{p[1]:.0f}) 未命中输入框 -> 计数未清零")
                    else:
                        # 看不清是不是输入框：不猜、不清零。
                        # 若模型随后仍要「连打第二次」，should_block 会升级为「停下报告」。
                        self._unknown_click = True
                        self.last_reset_note = (
                            f"CLICK ({p[0]:.0f},{p[1]:.0f}) 输入框判断不明 -> 计数未清零")
                else:
                    # 没去查（当时没有待清的计数）：清掉过期的「看不清」标记
                    self._unknown_click = False
                    self.last_reset_note = (
                        f"CLICK ({p[0]:.0f},{p[1]:.0f}) 未检查（无待清计数）")

                self.clicks.append(p)
                self.clicks = self.clicks[-STUCK_WINDOW:]
                self._click_fps.append(screen_fp)
                self._click_fps = self._click_fps[-STUCK_WINDOW:]
                n = self._count_stuck_clicks()
                if n >= STUCK_REPEAT_THRESHOLD:
                    return (f"最近 {len(self.clicks)} 次点击里有 {n} 次打在附近同一处"
                            f"**且界面没变**（容差 {STUCK_POSITION_TOLERANCE_PX}px）"
                            f"—— 点不动，多半是目标控件没响应或已被遮挡")

        elif name == "TYPE":
            text = str(action.get("value", ""))
            if text:
                self.consecutive_type += 1

                # ① 连续 TYPE：交给 should_block() 处理（跳过这一步，不终止任务），
                #    这里**不再**返回卡死原因 —— 否则 update 会先终止任务，
                #    拦截后继续尝试点发送按钮的机会就没了。
                #    实测证据：模型点不中发送按钮时会反复追加文字，退化成乱码。

                # ② 兼容原有：连续输入完全相同的内容
                self.types.append(text)
                self.types = self.types[-STUCK_TYPE_REPEAT_THRESHOLD:]
                if (self.consecutive_type > MAX_CONSECUTIVE_TYPE):
                    pass  # 已由 should_block 拦截，本轮不判卡死
                elif (len(self.types) >= STUCK_TYPE_REPEAT_THRESHOLD
                        and len(set(self.types)) == 1):
                    return (f"连续 {STUCK_TYPE_REPEAT_THRESHOLD} 次输入同一段文字"
                            f"「{redact_text(text)}」仍未推进 —— 输入很可能没真正上屏"
                            f"（yadb 静默失败的典型症状）")

        elif name == "SLIDE":
            p1, p2 = action.get("point1"), action.get("point2")
            if p1 is not None and p2 is not None:
                self.slide_total += 1        # ★ 整局累计（2026-10-01）
                a, b = parse_point(p1), parse_point(p2)
                self.slides.append((a, b))
                self.slides = self.slides[-STUCK_WINDOW:]
                if len(self.slides) >= STUCK_SLIDE_REPEAT_THRESHOLD:
                    # 起点、终点都在容差内算同一次滑动
                    n = 0
                    for s1, s2 in self.slides:
                        if self._same_spot(s1, a) and self._same_spot(s2, b):
                            n += 1
                    if n >= STUCK_SLIDE_REPEAT_THRESHOLD:
                        return (f"最近 {len(self.slides)} 次滑动里有 {n} 次几乎相同"
                                f"—— 页面没滚动，滑动无效")

        # 其余动作（BACK/HOME/WAIT/COMPLETE）不参与卡死判定，
        # 也**不清空**任何窗口 —— 这正是旧实现的第 2 个缺陷。
        return None

    def should_block(self, action: dict[str, Any]) -> bool:
        """
        这个动作是否应该**拒绝执行**（不是终止任务，只是跳过）。

        与 update() 的区别：update 返回原因时表示「该收工了」；
        should_block 表示「这一步别做，但可以继续想别的办法」。
        连续 TYPE 属于后者 —— 拦掉这次，模型还有机会去点发送按钮。

        ★ 主循环的调用顺序（2026-09-29 核对源码确认）：
            stuck_reason = stuck.update(action)     # 先把本步计入
            ...
            if stuck.should_block(action):          # 再看该不该拦
        也就是说 should_block 被调用时，consecutive_type **已经包含本步**，
        这里直接用 `>` 比较即可。

        ★ 曾经写错（2026-09-29 真机 + 单测双重定位）：误以为顺序是
        should_block 在前，于是写成 `(self.consecutive_type + 1) > MAX`，
        结果**连第一次 TYPE 都被拦** —— 因为 update 已经加过 1，再加 1 就超了。
        症状：模型每次「点输入框 -> 打字」都打不进去，只剩空转到步数耗尽。
        当时之所以没发现，是修复用的单测没模拟 `continue`（被拦的步不喂给 update），
        两次错误互相抵消。教训：模拟主循环的测试必须连控制流一起模拟。

        MAX_CONSECUTIVE_TYPE = 1 的语义是「最多容忍 1 次」：
          本步是第 1 次 -> 1 > 1 假 -> 放行 ✅
          本步是第 2 次 -> 2 > 1 真 -> 拦截 ✅
        """
        name = str(action.get("action_type", "")).upper()
        self.escalate_stop = False
        if name == "TYPE":
            block = self.consecutive_type > MAX_CONSECUTIVE_TYPE
            # ★ 若这次该拦、且「上一次点击是不是输入框」判断不出来 —— 升级为「停下报告」。
            #   为什么：判断不出来通常是界面读不透（Flutter/WebView），此时小模型极易
            #   反复 TYPE 把文字叠成乱码；与其默默跳过，不如让用户接手（2026-10-04）。
            self.escalate_stop = block and self._unknown_click
            return block
        if name == "SLIDE":
            # ★ 滑动总次数超限（2026-10-01 加）：拦掉这一步，逼模型改用别的手段。
            #   提示词说了「最多 1 次」它不听，只能代码兜底。
            return self.slide_total > MAX_SLIDE_TOTAL
        return False




def execute_action(action: dict[str, Any], real_size: tuple[int, int],
                   device: str | None, has_yadb: bool,
                   dry_run: bool = False,
                   prefer_point: tuple[float, float] | None = None) -> ExecResult:
    """
    把模型给出的动作翻译成 adb 命令并执行。

    坐标换算在这里做：模型给的是 0-1000 归一化值，adb 需要真实像素。
    换算公式与 GELab-Zero 官方一致 —— real = (norm / 1000) * 屏幕尺寸。

    :param prefer_point: 模型最近一次点击的**归一化**坐标（0-1000）。
                         TYPE 时用来判断「它想往哪个输入框打字」，
                         传 None 表示没有可参考的点击。
    """
    name = str(action.get("action_type", "")).upper()
    real_w, real_h = real_size

    def to_real(point: Any) -> tuple[int, int]:
        """
        把 0-1000 归一化坐标换成真实像素。

        ★ 越界**拒绝执行**，不再「夹回屏幕边」（2026-10-05）：
          旧实现 `min(max(...), w-1)` 会把明显越界的坐标**静默**拉到屏幕边缘 ——
          模型给 1040 就点在最右一列；SLIDE 两端都越界时被夹成**同一点**，
          `input swipe` 退化成原地长按，页面纹丝不动却报「滑动成功」。
          vision 早已声明「不再硬拉回屏幕边」，这里对齐：越界即抛
          InvalidActionError，由调用方转成「这一步失败」，交给模型重试。
          （末尾的 min 只用于消除浮点舍入导致的越界，不改变语义。）
        """
        nx, ny = parse_point(point)
        if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
            raise InvalidActionError(
                f"坐标 ({nx:g}, {ny:g}) 越界（超出 0-1000 归一化范围）—— 已拒绝执行"
                f"（不再夹回屏幕边缘），请重新给出坐标")
        x = min(int(round(nx / 1000 * real_w)), real_w - 1)
        y = min(int(round(ny / 1000 * real_h)), real_h - 1)
        return x, y

    if name == "COMPLETE":
        return ExecResult(True, "任务完成", finished=True,
                          result_text=str(action.get("value", "")))

    if name == "CLICK":
        try:
            x, y = to_real(action["point"])
        except InvalidActionError as exc:
            return ExecResult(False, str(exc))
        if not dry_run:
            tap(x, y, device)
        return ExecResult(True, f"点击 ({x}, {y})")

    if name == "LONGPRESS":
        try:
            x, y = to_real(action["point"])
        except InvalidActionError as exc:
            return ExecResult(False, str(exc))
        duration_ms = int(float(action.get("duration", DEFAULT_LONGPRESS_DURATION)) * 1000)
        if not dry_run:
            # 原地滑动 = 长按（adb 原生没有 longpress 子命令）
            swipe(x, y, x, y, duration_ms, device)
        return ExecResult(True, f"长按 ({x}, {y}) {duration_ms}ms")

    if name == "SLIDE":
        try:
            x1, y1 = to_real(action["point1"])
            x2, y2 = to_real(action["point2"])
        except InvalidActionError as exc:
            return ExecResult(False, str(exc))
        duration_ms = int(float(action.get("duration", DEFAULT_SLIDE_DURATION)) * 1000)
        if not dry_run:
            swipe(x1, y1, x2, y2, duration_ms, device)
        return ExecResult(True, f"滑动 ({x1},{y1}) -> ({x2},{y2})")

    if name == "TYPE":
        text = str(action.get("value", ""))
        # ★ 日志里只写**脱敏后**的正文（2026-10-05）：
        #   下面的 ExecResult.note 会被 runner 打进日志文件，而正文可能是
        #   密码 / 验证码 / 私密消息。喂给模型的 history 仍用原文（见 runner）。
        safe = redact_text(text)
        if dry_run:
            return ExecResult(True, f"输入「{safe}」")

        # ★ 方案 A（2026-09-30 加）：先确保输入框聚焦，再打字。
        #   实测踩坑：模型点了两下别的地方就直接 TYPE，输入框没聚焦，
        #   yadb 的字哪儿都不去 -> 连续 3 次判失败 -> 退出码 2 -> 用户看到
        #   「助手拒绝发消息」。受控复现证明差别只在聚焦（点一下 414ms 就上屏）。
        pref_real = None
        if prefer_point is not None:
            try:
                pref_real = to_real(prefer_point)
            except InvalidActionError:
                # 参考点越界（模型给的坐标在容差边缘）—— 忽略它即可，
                # 不该因此让整步失败（focus_editable_box 会退回「单个输入框」策略）
                pref_real = None
        _ready, _note, baseline, is_pwd, ambiguous = focus_editable_box(
            device, prefer_point=pref_real, verbose=True)

        # ★★ 多个输入框且无法确定目标 -> 停下询问用户先点击目标框（2026-10-04 加）
        #   不猜测 = 避免把字打进错误的框（打错框会被回读验证误判成功，危害更大）。
        #   ★ 必须给自定义 headline/conclusion（2026-10-05）：否则
        #     report_need_human 会用默认措辞打出「需要人工输入（密码）」——
        #     与真实原因（有多个输入框）**完全不符**，正是本项目最忌讳的「自己给错结论」。
        if ambiguous:
            return ExecResult(
                False,
                _note,
                need_human=True,
                human_reason="界面有多个输入框但模型未明确点击目标框",
                human_headline="[有多个输入框，请人工点击目标框]",
                human_conclusion=(
                    f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                    f"界面有多个输入框、无法确定往哪个打字，**不是脚本失败**\n"
                    f"       请在手机上点一下目标输入框，再重新发起任务。"))

        # ★★ 撞上密码框 -> 停下，交给人工（2026-10-01 加）
        #   为什么必须停：① 密码属于敏感信息，不该由脚本代输；
        #   ② 就算代输也过不去 —— Android 对密码框的 text 恒返回空串，
        #      回读验证必然三次全败，最后以退出码 2 收场，白跑一趟。
        #   与其让它失败，不如早点说清楚。
        if is_pwd:
            return ExecResult(
                False,
                "检测到密码输入框，已停止 —— 请人工输入密码",
                need_human=True,
                human_reason="模型准备往密码框里打字（该框 password=true）",
                human_headline="[涉及密码，请人工输入]",
                human_conclusion=(
                    f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                    f"需要人工输入密码，**不是脚本失败**\n"
                    f"       脚本已停在原处，请人工输完后再重新发起任务。"))

        # 带验证的输入：yadb 会**静默失败**（返回 code:0 但没上屏），
        # 所以不能信它的返回值，必须回读输入框确认。最多试 TYPE_RETRY 次。
        # 基线能复用就复用（省一次 ~1.7 秒的 dump），复用了不了一次才重新读。
        last_before = baseline if baseline is not None else read_focused_text(device)
        for attempt in range(1, TYPE_RETRY + 1):
            input_text(text, device, has_yadb)
            time.sleep(TYPE_VERIFY_DELAY)

            now = read_focused_text(device)
            if now is None:
                # 读不到「聚焦的 EditText」（Flutter/WebView/自绘界面不暴露 focused）
                # —— 无法验证，不误报失败，直接放行，但把话说清楚让人工去核对。
                print("[注意] 界面不暴露聚焦输入框，无法回读验证 —— "
                      "这一句是否真的上屏请人工确认")
                return ExecResult(True, f"输入「{safe}」（未能回读验证）")
            # ★ 成功判据收紧（2026-10-05）：旧写法第二句是
            #   「只要输入框内容变了就算成功」—— 于是**被 maxlength 截断**、
            #   或只打出前几个字，都会判成功，模型据此 COMPLETE → 发出半截消息。
            #   现在额外要求「框里的字数不少于我们输入的字数」：
            #     · 自动格式化（138 1234 5678）-> 归一化后仍包含、长度只多不少 ✓
            #     · 截断 / 部分上屏        -> 长度不足 -> 判失败，走重试 ✓
            norm_text, norm_now = _norm(text), _norm(now)
            if (norm_text in norm_now
                    or (last_before is not None and now != last_before
                        and len(norm_now) >= len(norm_text))):
                if attempt > 1:
                    return ExecResult(True, f"输入「{safe}」（第 {attempt} 次才成功）")
                return ExecResult(True, f"输入「{safe}」")

            # 没生效。先清掉输入框里可能挡路的残字再重试。
            info(f"[警告] TYPE 第 {attempt} 次没生效（输入框仍是「{now}」）")
            # ★ 只有「框里的内容和打字前不一样」才发退格（2026-09-30 修）：
            #   Android 对空 EditText 会把 hint 当 text 返回，所以空框上读到的
            #   是「搜索设置项」这类占位文字 —— 旧写法 `and now:` 会因此对一个
            #   **空框**连发 5 次 DEL，在某些 App 上会误触返回键行为。
            #   用 now != last_before 判断，语义正好是「这框里确实多了东西」。
            if attempt < TYPE_RETRY and now and now != last_before:
                adb("shell", "input", "keyevent", "123", device=device)  # MOVE_END
                # ★ 按实际长度清，且**一次调用带多个 keycode**（2026-10-05 修）：
                #   旧实现固定只发 20 个退格 —— 超过 20 字的消息清不干净，
                #   残留会被下一次重试**追加**在后面（还是半截 + 拼错）。
                #   `input keyevent` 支持一次传多个 keycode，批量发比逐个发快得多。
                n = min(len(now), TYPE_CLEAR_MAX)
                if n:
                    adb("shell", "input", "keyevent", *(["67"] * n), device=device)  # DEL × n
                time.sleep(0.3)
                last_before = read_focused_text(device)

        return ExecResult(False, f"输入「{safe}」连续 {TYPE_RETRY} 次都没上屏，已放弃")

    if name == "BACK":
        if not dry_run:
            keyevent(4, device)
        return ExecResult(True, "返回键")

    if name == "HOME":
        if not dry_run:
            keyevent(3, device)
        return ExecResult(True, "回桌面")

    if name == "WAIT":
        seconds = float(action.get("seconds", WAIT_SECONDS_DEFAULT))
        if not dry_run:
            # 保险：正常已在 normalize_action 校验过 0~WAIT_SECONDS_MAX，这里再夹一次防意外
            time.sleep(max(0.0, min(seconds, WAIT_SECONDS_MAX)))
        return ExecResult(True, f"等待 {seconds:g} 秒")

    if name == "OPEN":
        app = str(action.get("app", "")).strip()
        if not app:
            return ExecResult(False, "OPEN 动作缺少 app 参数")
        if dry_run:
            return ExecResult(True, f"启动应用「{app}」")
        try:
            pkg = launch_app(app, device)
        except AdbError as exc:
            return ExecResult(False, str(exc))
        return ExecResult(True, f"已启动「{app}」({pkg})")

    return ExecResult(False, f"不认识的动作：{name}")




def report_need_human(device: str | None, img: Image.Image, step: int,
                      reason: str,
                      headline: str | None = None,
                      conclusion: str | None = None) -> None:
    """
    遇到需要人工介入的场景时，把话说清楚并留证据（2026-10-01 加）。

    :param headline:   抬头行；不传则用「需要人工输入（密码）」的默认措辞
    :param conclusion: 结尾结论；不传则用「需要人工输入（密码）」的默认措辞
                       （2026-10-04 加：让「界面读不透」这类场景也能复用本函数）

    ★ 为什么末尾必须有一行无歧义的结论（2026-09-30 实测踩坑）：
      调用方（尤其是 LLM）往往**只读尾部**。光在中间写「已停止」不够，
      它照样会照着退出码自行解释，甚至编一个与事实无关的原因回给用户。
      所以结尾三件事都要写：是什么、不是什么、不许编什么。
    """
    if headline is None:
        headline = f"[需要人工输入] 第 {step} 步停下 —— 请你在手机上完成这一步"
    print(f"\n{'=' * 56}")
    print(headline)
    print(f"原因：{reason}")

    # current_package 现在会在 adb 失败时抛 AdbError（见其 docstring）——
    # 这里只是**信息性**读取（打印当前应用），掉线不该让「报告人工介入」本身也崩。
    try:
        pkg = current_package(device)
    except AdbError:
        pkg = ""
    if pkg:
        print(f"当前应用：{pkg}")

    # 留证据：事后要确认「到底停在哪个界面」
    try:
        shot = TMP_DIR / f"need_human_step{step}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        img.save(shot)
        print(f"现场截图已存：{shot}")
        prune_tmp()          # 只留最近 TMP_KEEP 张，防无限累积
    except Exception as exc:
        print(f"[警告] 截图保存失败（{exc}）—— 事后无法核对停在哪个界面")

    print("=" * 56)
    if conclusion is None:
        conclusion = (f"[结论] 任务【未完成】—— 退出码 {EXIT_NEED_HUMAN} = "
                      f"需要人工输入（密码），**不是失败**\n"
                      f"       脚本已停在原处，等你输完密码后可重新发起任务。")
    print(conclusion)
    print("       不要据此回复「任务失败」，也不要编造脚本没说过的原因。")
