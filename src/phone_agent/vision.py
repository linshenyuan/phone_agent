"""视觉模型交互 —— 提示词、动作解析、请求与重试。"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

from .config import COORD_RANGE_TOLERANCE, DEFAULT_LONGPRESS_DURATION, DEFAULT_SLIDE_DURATION, MODEL_RETRY, MODEL_RETRY_BACKOFF, WAIT_SECONDS_DEFAULT, WAIT_SECONDS_MAX
from .output import info
from .deps import Image, OpenAI
import re
import time



# ============================================================
# 模型交互
# ============================================================

PROMPT_TEMPLATE = """你是一个 Android 手机操作助手。你会看到手机当前的屏幕截图，需要根据任务目标决定下一步动作。

【任务】
{TASK}

★ 注意：上面的任务如果包含多个步骤，它们默认是**在同一个 App 内连续完成**的。
   不要把它们当成几个互不相关的任务。

【已执行的动作】
{HISTORY}

【输出要求】
只输出一行动作，不要输出解释、思考过程或任何其他文字。

坐标一律使用 0-1000 的归一化整数：屏幕左上角是 (0, 0)，右下角是 (1000, 1000)。
不要用像素坐标。

格式：action:<动作名>	<参数名>:<值>
（动作名与参数之间、参数与参数之间都用制表符分隔）

可用动作：
action:CLICK	point:<x>,<y>
action:SLIDE	point1:<x1>,<y1>	point2:<x2>,<y2>
action:TYPE	value:<要输入的文字>
action:LONGPRESS	point:<x>,<y>
action:OPEN	app:<应用名或包名>
action:BACK
action:HOME
action:WAIT
action:COMPLETE

注意：上面 <x>,<y> 这些是**占位符，不是可用的数字**。
你必须看当前截图，自己判断目标元素在哪，再把它换算成 0-1000 的坐标填进去。
**绝对不许**把占位符或任何示例数字原样照着输出。

【判断规则】
1. 坐标必须是 0-1000 的归一化值，点击要取元素中心
2. 目标不在当前屏幕时，先用 BACK 返回上级；只有确认目标在屏幕外才用 SLIDE
3. 输入文字前必须确保光标已在输入框内（先 CLICK 输入框）
4. 页面正在加载、或刚点击完还没跳转时，输出 WAIT
5. 任务达成后立即输出 COMPLETE，不要多余操作
6. 当前是桌面（主屏幕）且要用的 App 没打开时，**必须用 OPEN 启动它**，不要去点图标
7. 任务里出现多个动作时（例如「打开A，再打开B」），后面的动作通常是
   **当前 App 内部的功能**，默认在当前界面里找，不要退回桌面。
   「收藏」「设置」「我的」「消息」这类是**功能名，不是 App 名**，
   禁止用 OPEN 去启动它们。
8. ★ 如果任务**只是**「打开 / 启动某个 App」，而该 App 的界面**已经出现在当前
   截图上**，立即输出 COMPLETE —— 打开类任务在 App 出现的那一刻就算完成了，
   不要再点击、滑动或做任何多余操作。

【OPEN 用法 —— 桌面场景必读】
当前画面是桌面（主屏幕）时，**禁止**用 CLICK 去点 App 图标。原因：本机桌面是
自绘界面，元素读不出来，靠截图猜图标坐标基本会点错。正确做法是直接输出：

action:OPEN	app:微信
action:OPEN	app:com.tencent.mm

`app` 后面填**应用的中文名**（如 微信 / 设置 / 抖音）或**包名**都行。
系统会自己把名字换成包名并用 adb 启动，比点图标可靠得多。
只有目标 App 已经在前台时，才用 CLICK 在界面内部操作。

【输入文字的正确顺序 —— 很容易做错】
必须严格按「点输入框 -> 输文字 -> 点发送」三步走：
  第 1 步：CLICK 输入框（坐标取输入框中间）
  第 2 步：TYPE 你要说的话
  第 3 步：CLICK 发送按钮

发送按钮的定位：**自己看截图找**。它是当前界面里负责「把输入框内容发出去」的
那个控件，通常紧挨着输入框（一般在输入框右侧或右下角），外观上是按钮。
每款 App 的位置和样式都不一样，**不许照搬任何固定坐标**，必须根据当前这张截图
自己判断它在哪，然后点它的中心。

【TYPE 之后的硬性约束 —— 违反会导致文字叠成乱码】
TYPE 之后**必须立刻点发送按钮**。特别注意：
  1. **绝对不许连续 TYPE 两次**。输入框里的文字是**叠加**的，
     再 TYPE 一次会在原有文字后面接上，越接越长，最后变成一长串乱码。
  2. 如果点了发送按钮但消息没发出去，**不要**再 TYPE。
     正确做法：重新 CLICK 输入框 -> 用退格清空 -> 再重新 TYPE。
  3. 判断消息是否已发出：看聊天记录里**有没有出现你刚输入的这条消息**。
     出现了就是成功了，直接输出 COMPLETE，不要再做任何操作。
  4. 如果输入框里的文字已经超过一句正常的话（比如出现重复的长串），
     说明已经出错，**立即停止 TYPE**，改为清空重来。

【SLIDE 使用限制 —— 必须遵守】
SLIDE 是最后手段，一次任务最多用 1 次。以下情况**禁止**使用 SLIDE：
  1. 刚点完发送 / 提交 / 确认 —— 消息已经出现在屏幕上就是成功了，直接输出 COMPLETE
  2. 想确认上一步操作是否生效 —— 看一眼截图就能判断，不需要滑动
  3. 键盘弹出或收起导致画面位移 —— 这是正常现象，不要滑动
  4. 不确定下一步该做什么 —— 输出 WAIT，不要用滑动来"试探"
  5. 目标已经能看见 —— 直接点击它，不要先滑动

下一步动作："""




def build_history_text(history: list[dict[str, Any]], limit: int = 12) -> str:
    """
    把动作历史压成简短文本。

    只保留最近 limit 条，且不带截图 —— 否则上下文会被历史图片撑爆。
    """
    if not history:
        return "（还没有执行任何动作）"

    lines = []
    for i, act in enumerate(history[-limit:], 1):
        name = act.get("action_type", "?")
        if name in ("CLICK", "LONGPRESS"):
            desc = f"{name} {act.get('point')}"
        elif name == "SLIDE":
            desc = f"SLIDE {act.get('point1')} -> {act.get('point2')}"
            # ★ 附上滑动前的界面文字（2026-10-01 加）：让模型知道当时屏幕上有什么，
            #   从而判断目标是不是已经在了 —— 它只看截图时认不出语义。
            if act.get("screen_text"):
                desc += f"\n   （当时界面文字：{act['screen_text']}）"
        elif name == "TYPE":
            desc = f"TYPE 「{act.get('value', '')}」"
        elif name == "BLOCKED_TYPE":
            # 关键：要让模型知道上一次 TYPE 被拦了，否则它会以为是自己没执行成功
            desc = (f"TYPE 「{act.get('value', '')}」**被系统拦截，没有执行**"
                    f"（输入框里已有内容，禁止重复输入；请改为点击发送按钮）")
        elif name == "BLOCKED_SLIDE":
            desc = ("SLIDE **被系统拦截，没有执行**"
                    "（滑动次数已用尽；目标应该已经可见，请直接 CLICK 或输出 COMPLETE）")
        elif name == "OPEN":
            desc = f"OPEN 「{act.get('app', '')}」"
        elif name == "COMPLETE":
            desc = "COMPLETE"
        else:
            desc = name
        # ★ 失败标记（2026-10-05 加）：让模型明确看到上一步失败了，别再原样重试
        if act.get("failed"):
            desc = f"❌ 失败：{desc} —— {act.get('error', '')}（别原样重试，换个办法）"
        lines.append(f"{i}. {desc}")

    if len(history) > limit:
        lines.insert(0, f"（更早的 {len(history) - limit} 步已省略）")
    return "\n".join(lines)




def image_to_data_url(img: Image.Image) -> str:
    """把 PIL 图片编码成 OpenAI 接口接受的 data URL。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"




def resize_for_model(img: Image.Image, target_width: int) -> Image.Image:
    """把截图缩到目标宽度（只缩不放），保持宽高比。"""
    if img.width <= target_width:
        return img
    ratio = target_width / img.width
    return img.resize((target_width, round(img.height * ratio)), Image.LANCZOS)




class InvalidActionError(ValueError):
    """
    模型动作**值**不合法（坐标越界、TYPE 空值等）—— 与「格式不认得」区分开。

    ★ 为什么要单独一个类（2026-10-04）：parse_action 在 JSON 解析失败时会回落到
      key:value 再试一次。若把「坐标越界」也当成普通 ValueError，就会先被 JSON 分支
      吞掉、再拿同一段文本硬解析，最后报出「找不到动作名」这种**与事实不符**的错误。
      值非法时应当**直接判失败、交给 ask_model 重试重问**，不回落。
    """


def parse_point(value: Any) -> tuple[float, float]:
    """
    把坐标值解析成 (x, y) 归一化数值（0-1000）。

    模型可能给出三种写法，都要认：[500, 800] / (500, 800) / "500,800"。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    if isinstance(value, dict):
        return float(value.get("x", 0)), float(value.get("y", 0))
    parts = re.split(r"[,\s]+", str(value).strip().strip("[](){}"))
    if len(parts) >= 2:
        return float(parts[0]), float(parts[1])
    raise ValueError(f"无法解析坐标：{value!r}")




def _checked_point(value: Any, label: str) -> tuple[float, float]:
    """
    解析坐标并做范围校验（2026-10-04 加）。

    模型报位置用的是 0-1000 的比例数（0=最左/最上，1000=最右/最下）。
    允许 ±COORD_RANGE_TOLERANCE 的小幅越界（容忍四舍五入），
    明显越界（如 -100 / 99999）则判非法 —— 抛错交给 ask_model 重试重问。

    ★ 为什么不再「硬拉回屏幕边」：那样会在模型明显给错坐标时仍然点下去，
      而且点到的位置和模型本意无关，后续行为极难排查（2026-10-04 用户反馈）。
    """
    p = parse_point(value)
    lo, hi = -COORD_RANGE_TOLERANCE, 1000 + COORD_RANGE_TOLERANCE
    if not (lo <= p[0] <= hi and lo <= p[1] <= hi):
        raise InvalidActionError(
            f"{label} 坐标越界：{p} —— 应在 0-1000 之间"
            f"（允许 ±{COORD_RANGE_TOLERANCE} 误差），已拒绝执行，请重新给出坐标")
    return p




def normalize_action(obj: dict[str, Any]) -> dict[str, Any]:
    """
    把模型给出的动作统一成内部格式。

    内部格式固定为「大写动作名 + 0-1000 归一化坐标」：
        {"action_type": "CLICK", "point": (500.0, 800.0)}

    同时兼容三种来源的键名，因为模型三种都可能吐出来：
      * GELab-Zero 官方：action_type / point / value
      * 官方 uiTars 变体：action / coordinate
      * 我早期写的：action / x / y

    ★ 坐标做范围校验（2026-10-04 加）：明显越界（如 -100 / 99999）抛错，
      交给 ask_model 重试重问，而不是留到 to_real 里偷偷夹回屏幕。
    """
    name = str(
        obj.get("action_type") or obj.get("action") or obj.get("type") or ""
    ).strip().upper()
    if not name:
        raise ValueError(f"模型输出里找不到动作名：{obj}")

    out: dict[str, Any] = {"action_type": name}

    if name in ("CLICK", "LONGPRESS"):
        point = obj.get("point")
        if point is None:
            point = obj.get("coordinate")
        if point is None and "x" in obj and "y" in obj:
            point = (obj["x"], obj["y"])
        out["point"] = _checked_point(point, name)
        if name == "LONGPRESS":
            out["duration"] = float(obj.get("duration", DEFAULT_LONGPRESS_DURATION))

    elif name in ("SLIDE", "SCROLL"):
        # 官方把 Scroll 映射成 SLIDE
        out["action_type"] = "SLIDE"
        p1, p2 = obj.get("point1"), obj.get("point2")
        if p1 is None and "x1" in obj and "y1" in obj:
            p1 = (obj["x1"], obj["y1"])
        if p2 is None and "x2" in obj and "y2" in obj:
            p2 = (obj["x2"], obj["y2"])
        out["point1"] = _checked_point(p1, "SLIDE 起点")
        out["point2"] = _checked_point(p2, "SLIDE 终点")
        out["duration"] = float(obj.get("duration", DEFAULT_SLIDE_DURATION))

    elif name == "TYPE":
        out["value"] = str(obj.get("value") or obj.get("text") or "")
        # ★ 空 value 必须判无效（2026-09-30 审计）：
        #   回读验证的判据是 `text in now`，而空串 `"" in 任意文字` 恒为 True
        #   -> 恒判成功；同时 StuckDetector 用 `if text:` 跳过空 TYPE -> 计数不增长。
        #   两者叠加 = 模型可以无限输出空 TYPE，每步都"成功"且不触发任何保护，
        #   直到步数烧完。抛错交给 ask_model 的重试接住，比让它静默空转好。
        if not out["value"]:
            raise InvalidActionError("TYPE 动作的 value 为空 —— 模型没给出要输入的文字")

    elif name == "WAIT":
        # ★ 秒数做范围校验（2026-10-04 加）：负数会让 time.sleep 抛错、
        #   过大没有意义，一律判非法交给重试，而不是悄悄夹到 10。
        raw = obj.get("seconds")
        if raw is None:
            raw = obj.get("duration")
        if raw is None:
            raw = WAIT_SECONDS_DEFAULT
        try:
            secs = float(raw)
        except (TypeError, ValueError):
            raise InvalidActionError(f"WAIT 秒数无法解析：{raw!r}")
        if not (0 <= secs <= WAIT_SECONDS_MAX):
            raise InvalidActionError(
                f"WAIT 秒数非法：{secs:g} —— 应在 0-{WAIT_SECONDS_MAX} 秒之间"
                f"（负数会让 sleep 抛错、过大无意义），已拒绝执行，请重新给出")
        out["seconds"] = secs

    elif name in ("OPEN", "LAUNCH", "START_APP"):
        # 打开应用：交给 adb 直接启动，不走桌面图标识别（vivo 桌面读不到元素）
        out["action_type"] = "OPEN"
        out["app"] = str(
            obj.get("app") or obj.get("value") or obj.get("package")
            or obj.get("name") or ""
        ).strip()

    return out




def parse_action(text: str) -> dict[str, Any]:
    """
    解析模型输出的动作。

    GELab-Zero 的原生输出是制表符分隔的 key:value：
        action:CLICK	point:500,800
    但官方框架也吃 JSON，所以这里两条路都试：先 JSON，再 key:value。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回了空内容")

    # 剥掉 markdown 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    # 路径一：JSON（截取第一个 { 到最后一个 }）
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            if isinstance(obj, dict):
                return normalize_action(obj)
        except InvalidActionError:
            raise  # ★ 值不合法（坐标越界 / TYPE 空值）：直接判失败、重试重问，不回落
        except (json.JSONDecodeError, ValueError, TypeError):
            pass  # 格式问题：落到下面的 key:value 路径

    # 路径二：action:CLICK<TAB>point:500,800
    # ★ 模型未必遵守「制表符分隔」—— 实测 4B 常输出空格，而旧实现会把整段
    #   当成动作名：`action:CLICK point:500,800` -> `{'action':'CLICK point:500,800'}`
    #   -> execute_action 返回「不认识的动作」-> 白烧一步。
    #   所以先把「键: 之前」的空白统一成制表符，再按原逻辑切分。
    #   (?<!:) 是必须的守卫：否则 `value: http://x` 会被切成 `value:` + `http://x`。
    normalized = re.sub(r"(?<!:)\s+(?=[A-Za-z_][A-Za-z0-9_]*\s*:)", "\t", raw)
    fields: dict[str, str] = {}
    for chunk in re.split(r"[\t\n]+", normalized):
        chunk = chunk.strip().strip(",").strip()
        if not chunk or ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        fields[key.strip().lower()] = value.strip()

    if not fields:
        raise ValueError(f"模型输出既不是 JSON 也不是 key:value 格式：{raw[:200]}")
    return normalize_action(fields)




def build_http_client() -> Any:
    """
    构造一个**不走系统代理**的 HTTP 客户端。

    必要性（实测踩坑）：httpx2 默认 `trust_env=True`，会读 `http_proxy` 环境变量。
    本机存在系统代理（`http_proxy=http://127.0.0.1:62801`），于是发往本地模型的
    请求被塞给代理转发 —— 2.5 MB 的带图 POST 被代理中止（ReadError 10053），
    SDK 重试后才成功，有时干脆吃回 `404 File Not Found`。
    本地推理服务永远不该走代理，这里显式关掉。

    注意：curl / urllib 对 127.0.0.1 有内建 bypass，所以它们从来不走代理 ——
    只有 httpx 系老实遵守环境变量，这也是这个坑难查的原因。

    :return: httpx2.Client；拿不到 httpx2 时返回 None，交给 SDK 用默认客户端
    """
    try:
        import httpx2
    except ImportError:
        return None
    return httpx2.Client(trust_env=False)




def served_model_ids(models_obj: Any) -> list[str]:
    """从 `/v1/models` 的响应里取出服务端**实际可用**的模型 id 列表。"""
    out: list[str] = []
    for m in getattr(models_obj, "data", None) or []:
        mid = getattr(m, "id", None)
        if mid:
            out.append(str(mid))
    return out




def model_name_matches(want: str, available: list[str]) -> bool:
    """
    配置的模型名是否对得上服务端提供的某个模型。

    ★ 为什么需要「宽松匹配」（2026-09-30 实测踩坑）：
      `start-api.ps1` 在**不指定 -Alias 时**会从模型文件名推导别名 ——
      `stepfun-ai_GELab-Zero-4B-preview-Q6_K.gguf` 会被剥掉量化后缀，
      得到 `stepfun-ai_GELab-Zero-4B-preview`。
      而本脚本默认发的是 `GELab-Zero`，两边对不上。
      所以这里做三级宽松匹配：完全相等 → 忽略大小写相等 → 一方包含另一方。
    """
    w = (want or "").strip().lower()
    if not w:
        return False
    for a in available:
        al = a.lower()
        if w == al or w in al or al in w:
            return True
    return False




def _ask_once(client: OpenAI, model: str, view_img: Image.Image,
              task: str, history: list[dict[str, Any]],
              timeout_hint: bool = True) -> dict[str, Any]:
    """发一次请求并解析成动作。失败直接抛异常，由 ask_model 决定是否重试。"""
    prompt = (PROMPT_TEMPLATE
              .replace("{TASK}", task)
              .replace("{HISTORY}", build_history_text(history)))

    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_to_data_url(view_img)}},
            ],
        }],
        temperature=1.0,
        top_p=0.95,
        # 每步只输出一个短动作，256 绰绰有余。
        # 别调大：实测 openai 3.17.0 在 max_tokens=2048 时会报
        # 404 File Not Found，而 llama-server 日志里根本收不到该请求
        # （裸 HTTP 发同样的 2048 则正常 —— 是 SDK 侧的问题）。
        max_tokens=256,
    )

    msg = resp.choices[0].message
    content = (msg.content or "").strip()

    if not content:
        # 思考型模型把内容放在 reasoning_content 且 content 为空，通常是 max_tokens 不够
        reasoning = getattr(msg, "reasoning_content", "") or ""
        if timeout_hint and reasoning:
            raise ValueError(
                "模型只产出了思考内容、没有最终答案 —— max_tokens 不够，"
                "请调大 max_tokens 或缩短提示词"
            )
        raise ValueError("模型返回了空内容")

    return parse_action(content)




def ask_model(client: OpenAI, model: str, view_img: Image.Image,
              task: str, history: list[dict[str, Any]],
              timeout_hint: bool = True) -> dict[str, Any]:
    """
    把当前截图 + 任务 + 历史动作发给模型，取回下一步动作。

    temperature 固定 1.0 —— GELab-Zero 官方指南明确要求，调低会让策略过于保守卡住不动。

    ★ 正因为 temperature=1.0，重采样是有意义的（2026-09-30 审计）：
      坏输出（空内容 / 解析失败 / value 为空的 TYPE / 网络抖动）重试 1~3 次
      通常就能拿到合法动作，不该让一次坏输出终结整个任务。
      已知取舍：4xx 类确定性错误（如模型名写错）也会重试，白等约 3 秒 ——
      这是刻意的简化，区分状态码要引入 openai.APIStatusError，代码变多、收益很小。
    """
    last: Exception | None = None
    for attempt in range(1, MODEL_RETRY + 1):
        try:
            return _ask_once(client, model, view_img, task, history, timeout_hint)
        except Exception as exc:
            last = exc
            if attempt < MODEL_RETRY:
                wait = MODEL_RETRY_BACKOFF * attempt
                info(f"[重试] 第 {attempt}/{MODEL_RETRY} 次模型调用失败：{exc}"
                     f"  —— {wait:g}s 后重采样")
                time.sleep(wait)
    assert last is not None
    raise last
