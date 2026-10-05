"""界面树解析 —— dump XML、解析节点、找输入框、提取可见文字。"""

from __future__ import annotations

import time
from typing import Any

from .adb import AdbError, adb, tap
from .config import TYPE_FOCUS_TAP_DELAY, _UI_DUMP_DIR
from .output import info
import os
import re



def _ui_dump_path() -> str:
    """本次进程独享的 dump 文件名 —— 上一轮被 kill 留下的残留绝不会被读到。"""
    return f"{_UI_DUMP_DIR}/_agent_dump_{os.getpid()}.xml"




def _dump_ui(device: str | None = None) -> str | None:
    """
    dump 一次界面树，返回 XML；拿不到有效内容返回 None。

    ★ 顺序与判据都是踩过坑的（2026-09-30 审计）：
      1. `uiautomator dump` **即使失败也返回 exit 0 并打印
         "UI hierchary dumped to: ..."** —— 实测 dump 到 /proc/_nope.xml
         （不可能写入的路径）也这么说。所以 **stdout 不是成败信号**。
      2. 判据改用「cat 能不能读到」：dump 失败就不会产出文件，cat 会以非 0
         退出 -> AdbError -> None（实测 exit=1，可用）。
      3. 旧顺序是 dump -> cat -> rm。一旦上一轮的 rm 没执行到（Pi 侧 5 分钟
         超时被 kill 正好卡在中间），dump 再失败就会 cat 到**上一轮的旧界面树**，
         于是基于陈旧信息决定「点哪个输入框 / 输入框里是什么」。
         现在文件名带 pid + 先 rm，双保险。
    """
    path = _ui_dump_path()
    try:
        adb("shell", "rm", "-f", path, device=device)
        adb("shell", "uiautomator", "dump", path, device=device)
        return str(adb("shell", "cat", path, device=device))
    except AdbError:
        return None
    finally:
        try:
            adb("shell", "rm", "-f", path, device=device)
        except AdbError:
            pass




def _xml_unescape(s: str) -> str:
    """把 uiautomator dump 的 XML 实体还原成真实字符。"""
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))




def _parse_bounds(raw: str) -> tuple[int, int, int, int] | None:
    """把 '[x1,y1][x2,y2]' 解析成 (x1, y1, x2, y2)；解析不出来返回 None。"""
    m = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", (raw or "").strip())
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2




def _is_editable(cls: str) -> bool:
    """是不是「能打字」的控件。EditText 及其子类、自动补全框都算。"""
    return "EditText" in cls or "AutoComplete" in cls




def _parse_ui_nodes(xml: str) -> list[dict[str, Any]]:
    """
    把 dump 出来的 XML 解析成节点列表。

    注意：XML 声明里写的是 UTF-8，简单的否定字符类匹配不了中文属性值，
    所以不用 xpath，直接正则抓属性再做 XML 反转义。
    """
    nodes: list[dict[str, Any]] = []
    for m in re.finditer(r"<node\b[^>]*>", xml):
        tag = m.group(0)

        def attr(name: str) -> str:
            mm = re.search(rf'{name}="([^"]*)"', tag)
            return _xml_unescape(mm.group(1)) if mm else ""

        nodes.append({
            "cls": attr("class"),
            "text": attr("text"),
            "focused": 'focused="true"' in tag,
            "focusable": 'focusable="true"' in tag,
            # ★ 密码框标记（2026-10-01 加）：dump 里每个节点都带这个属性。
            #   用途是**识别并停下来**，不是自动填 —— 密码一律留给人工输入。
            "password": 'password="true"' in tag,
            "bounds": _parse_bounds(attr("bounds")),
        })
    return nodes




def _focused_editable_node(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    取当前「聚焦的可输入节点」本身；没有就返回 None。

    ★ 为什么要把「取节点」和「取文字」拆开（2026-10-01）：
      「有没有聚焦的输入框」和「读它的内容」是两件事。密码框属于
      「有聚焦、但内容读不到」，用旧的单函数签名无法表达。
    """
    for n in nodes:
        if n["focused"] and _is_editable(n["cls"]):
            return n
    return None




def _focused_editable_text(nodes: list[dict[str, Any]]) -> str | None:
    """
    从节点列表里取「聚焦的 EditText」的文字；没有就返回 None。

    ★ 密码框返回 None（2026-10-01 改）：Android 对 password 框的 text 恒返回空串，
      空串会被下游判成「读到了、但内容对不上」→ TYPE 三次重试全败 → 退出码 2。
      返回 None 才是「无法验证 → 放行」。
      （实际到不了那一步 —— TYPE 撞上密码框时会先被拦下来停下，见 execute_action。）
    """
    n = _focused_editable_node(nodes)
    if n is None:
        return None
    if n.get("password"):
        return None
    return n["text"]




def visible_texts(device: str | None = None, limit: int = 15,
                  max_len: int = 24) -> list[str]:
    """
    dump 一次，提取当前界面上**可见的文字**，供模型判断「目标是不是已经在了」。

    ★ 为什么需要（2026-10-01 实测）：4B 模型只看截图时认不出界面语义 ——
      任务「打开轻小说文库 / 打开收藏」里，App 启动就在「我的收藏」页，
      但它读不懂「我的收藏」这几个字意味着任务已完成，连滑 3 次才敢 COMPLETE。
      把界面文字直接喂给它，比让它对着像素猜有效得多。

    ★ 成本：一次 dump 实测约 2.4 秒（4 趟 adb 往返），所以只在 SLIDE 前调用，
      不做成每步都跑。

    :param limit: 最多返回多少条（防刷屏、控 token）
    :param max_len: 每条最长多少字符，超出截断加省略号
    :return: 去重后的可见文字列表；拿不到界面树时返回空列表
    """
    xml = _dump_ui(device)
    if xml is None:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for n in _parse_ui_nodes(xml):
        t = (n["text"] or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t if len(t) <= max_len else t[:max_len] + "…")
        if len(out) >= limit:
            break
    return out




def read_focused_text(device: str | None = None) -> str | None:
    """
    读当前**聚焦的输入框**里的文字，用来验证 TYPE 到底有没有生效。

    ★ 只认「聚焦的 EditText」（2026-09-30 修，方案 C）：
      旧实现会返回**任意 class** 的 focused 节点（按钮、列表项），
      还会退而取「第一个 EditText」即使是未聚焦的 —— 这两条都可能读到
      一个文字永不变化的节点，导致 TYPE 验证**恒判失败**，把本来在正常推进的
      任务冤杀（实测代价：QQ 发消息被误判失败并提前终止）。
      现在拿不到聚焦的 EditText 就返回 None，调用方按「无法验证」放行。

    :return: 聚焦输入框的文字（可能是空串；注意 Android 对空 EditText 会把
             hint 当作 text 返回，所以空框上读到的往往是占位提示文字）；
             没有聚焦输入框时返回 None（**不代表输入失败**）
    """
    xml = _dump_ui(device)
    if xml is None:
        return None
    return _focused_editable_text(_parse_ui_nodes(xml))




def _pick_editable(nodes: list[dict[str, Any]],
                   prefer_point: tuple[float, float] | None = None
                   ) -> dict[str, Any] | None:
    """
    从节点里挑一个「该被打字的输入框」。规则刻意保守，避免把字打进错误的框：

      * 单个输入框 -> 直接点它（聊天输入框 / 搜索框等绝大多数场景，命中率接近 100%）
      * 多个输入框 -> 仅当 prefer_point（模型上一步 CLICK 的真实坐标）落在某个框内时才点它
      * 多个输入框且坐标不命中任何框 -> 返回 None，**不猜测**（交给调用方停下询问）

    ★★ 不再用「面积最大」兜底：那是纯猜测，登录页 / 设置页 / 聊天+搜索页会选错框，
       而且打错框会被回读验证误判成「成功」，危害比「没聚焦」更大。

    :param prefer_point: 真实像素坐标；传 None 表示没有可参考的点击（上一步不是 CLICK）
    """
    edits = [n for n in nodes if _is_editable(n["cls"]) and n["bounds"]]
    if not edits:
        return None
    # 单个输入框：安全，直接点
    if len(edits) == 1:
        return edits[0]
    # 多个输入框：只有模型明确点过其中某个时，才帮它聚焦
    if prefer_point is not None:
        px, py = prefer_point
        for n in edits:
            x1, y1, x2, y2 = n["bounds"]
            if x1 <= px <= x2 and y1 <= py <= y2:
                return n
    # 多框且无明确目标：不猜，返回 None
    return None




def focus_editable_box(device: str | None = None,
                       prefer_point: tuple[float, float] | None = None,
                       verbose: bool = True
                       ) -> tuple[bool, str, str | None, bool, bool]:
    """
    ★ 方案 A（2026-09-30 加）：TYPE 之前先确保有一个**聚焦**的输入框。

    为什么必须做（实测）：模型常常点两下别的地方就直接 TYPE，这时输入框没聚焦，
    yadb 打进去的字**哪儿都不去**（它静默失败：返回 code:0 但没上屏），
    连续 3 次后脚本以退出码 2 提前终止 —— 用户看到的就是「助手拒绝发消息」。
    受控复现：先点搜索框再 `input_text("蓝牙")`，414 ms 就上屏了；
    差别只在「输入框有没有聚焦」。

    策略保守，避免把字打进错误的框：
      * 已经聚焦 -> 什么都不做
      * 界面上只有一个输入框 -> 点它中心（绝大多数场景：聊天输入框、搜索框）
      * 多个输入框 -> 仅当模型「上一步 CLICK」的坐标落在某个框内时才点它
      * 多个输入框且坐标不命中任何框 -> **不猜测**，返回歧义标记，由调用方停下询问
      * 一个输入框都没有（Flutter/WebView/自绘界面）-> 不动手，交给调用方按原逻辑处理

    ★★ 2026-10-04：取消「面积最大」兜底。那是纯猜测，登录/设置/聊天+搜索页会选错框，
       而且打错框会被回读验证误判成功，比「没聚焦」危害更大。歧义时改为显式停下。

    ★ 关于 focusable：**不能把它当硬条件**。实测 vivo 设置的搜索框
      `focusable="false"`（但它确实能被点开、能打字），所以这里用
      「优先 focusable，没有就退回全部 EditText」的兜底。

    ★★ 2026-10-01 改动：返回值增加第 4 个元素「聚焦的是不是密码框」。
      用途是让调用方**停下来等人工输入**，不是自动填充。
      同时把「是否已聚焦」的判断从 _focused_editable_text 换成
      _focused_editable_node —— 旧写法会把「密码框已聚焦」误判成「没有聚焦」，
      从而对一个已经聚焦的密码框再多点一下。

    :return: (是否已就绪, 说明文字, 聚焦输入框的当前文字, 是否密码框, 是否歧义)
             第五个元素 `ambiguous=True` 表示「多个输入框且无法确定目标」，
             调用方应停下询问用户先点击目标框，而不是猜一个。
             第三个元素**只在「本来就已聚焦且不是密码框」时非 None** —— 调用方可以直接拿它
             当 TYPE 前的基线，省掉一次 dump（实测一次约 1.7 秒）。
             点了输入框之后界面会重排（实测 bounds 从 (156,288,1020,384) 变成
             (156,108,879,204)），所以那种情况下必须重新 dump，这里返回 None。
    """
    xml = _dump_ui(device)
    if xml is None:
        return True, "读不到界面树，跳过聚焦检查", None, False, False

    nodes = _parse_ui_nodes(xml)
    edits = [n for n in nodes if _is_editable(n["cls"]) and n["bounds"]]
    if not edits:
        return True, "界面上没有可输入框，跳过聚焦检查", None, False, False

    already = _focused_editable_node(nodes)
    if already is not None:
        if already.get("password"):
            return True, "聚焦的是密码框", None, True, False
        return True, "输入框已聚焦", already["text"], False, False

    target = _pick_editable(nodes, prefer_point)
    if target is None:
        # 走到这里基本是「多个输入框 + 坐标不命中任何框」—— 歧义，不猜。
        # 交给调用方停下询问用户先点击目标框，避免把字打进错误的框。
        if len(edits) > 1:
            return (False,
                    "检测到多个输入框但无法确定目标，请先点击要输入的框再 TYPE",
                    None, False, True)
        return True, "没挑出可点的输入框，跳过", None, False, False

    x1, y1, x2, y2 = target["bounds"]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    is_pwd = bool(target.get("password"))
    if is_pwd:
        # ★ 不点。点了就等于替用户决定「往密码框里打字」—— 密码一律留给人工。
        #   直接把「这是密码框」报回去，让调用方停下。
        return True, f"目标输入框是密码框（({cx}, {cy})），未点击", None, True, False

    tap(cx, cy, device)
    time.sleep(TYPE_FOCUS_TAP_DELAY)
    if verbose:
        extra = "" if len(edits) == 1 else f"（界面上有 {len(edits)} 个输入框）"
        info(f"[聚焦] 输入框未聚焦，已点击其中心 ({cx}, {cy}){extra}")
    return True, f"已点击输入框 ({cx}, {cy})", None, False, False




def point_hits_editable(device: str | None,
                        real_point: tuple[float, float],
                        margin_px: int = 24) -> bool | None:
    """
    ★ 2026-10-04 加：判断一个**真实像素**坐标是不是压在某个输入框上。

    用途：给「连续 TYPE 保护」当清零判据 —— 只有模型真的点回输入框，才允许它
    再打一次字。取代旧的「看 y 是否在屏幕下方」的猜测（换机型 / 横屏 / 浮窗会猜错）。

    :param real_point: 真实像素坐标 (x, y)
    :param margin_px:  容差；模型常常点得略偏，放宽这么多像素仍算命中
    :return: True  —— 命中某个输入框
             False —— 界面上有输入框，但这个坐标没压住任何一个
             None  —— 判断不了：读不到界面树，或界面根本没暴露输入框
                      （Flutter / WebView / 自绘界面，程序只能看到一张图）
    """
    xml = _dump_ui(device)
    if xml is None:
        return None
    nodes = _parse_ui_nodes(xml)
    edits = [n for n in nodes if _is_editable(n["cls"]) and n["bounds"]]
    if not edits:
        return None
    x, y = real_point
    for n in edits:
        x1, y1, x2, y2 = n["bounds"]
        if (x1 - margin_px <= x <= x2 + margin_px
                and y1 - margin_px <= y <= y2 + margin_px):
            return True
    return False
