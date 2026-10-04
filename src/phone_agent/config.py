"""所有可调常量 —— 改这里就能调整 agent 的行为。"""

from __future__ import annotations

import os
import sys
from pathlib import Path




# ============================================================
# 默认配置
# ============================================================

DEFAULT_API_BASE = "http://127.0.0.1:8080/v1"


DEFAULT_API_KEY = "local"


DEFAULT_MODEL = "GELab-Zero"



# 喂给模型的图片宽度。手机原生 1080 宽，缩到 720 既省视觉 token 又不至于看不清小字。
# 调小 -> 更快更省显存，但小控件会糊；调大 -> 相反。
DEFAULT_VIEW_WIDTH = 720



DEFAULT_MAX_STEPS = 25



# 每步之间的固定等待（秒）：给 UI 动画/页面跳转留时间，避免截到过渡帧。
# 2026-09-30 实测：1.2 -> 0.4 秒，单步省 0.8 秒（约占单步总耗时的 9%）。
# 页面跳转动画一般 300~500 ms，0.4 秒是「够用又不浪费」的下限；
# 若某台设备/App 动画偏慢导致截到过渡帧，用 --step-delay 单独调大即可。
DEFAULT_STEP_DELAY = 0.4



# 滑动时长（秒）。0.4 秒接近人手快速滑动的手感；
# 原值 1.5 秒太慢，会被部分 App 识别成"拖拽"而不是"滚动"（抖音可能触发下拉刷新）。
DEFAULT_SLIDE_DURATION = 0.4



# 长按时长（秒）。必须 ≥ 0.5 秒，否则 Android 不认长按 —— 别跟着 SLIDE 一起调小。
DEFAULT_LONGPRESS_DURATION = 1.5



# ------------------------------------------------------------
# 卡死检测参数
# ------------------------------------------------------------
# 判定「同一个位置」的像素容差。模型每次算出的坐标有 ±1~3 的抖动
# （实测出现过 868 / 869 / 867），用「完全相等」判据永远不会触发 —— 这是个死判据。
STUCK_POSITION_TOLERANCE_PX = 30



# 滑动窗口大小：只看最近这么多次同类动作
STUCK_WINDOW = 4



# 窗口内有多少次算「同一个位置」就判定卡死
STUCK_REPEAT_THRESHOLD = 3



# 连续这么多次 TYPE 输入完全相同的内容，判定为输入无效（yadb 静默失败的典型表现）
STUCK_TYPE_REPEAT_THRESHOLD = 3



# 连续这么多次 SLIDE 几乎相同，判定为无效滑动（模型在原地试探）
STUCK_SLIDE_REPEAT_THRESHOLD = 3



# ★ 整局滑动总次数上限（2026-10-01 加）
#   为什么需要：提示词里写了「SLIDE 一次任务最多用 1 次」，但 4B 模型不遵守 ——
#   实测「打开轻小说文库 / 打开收藏」时它连滑 3 次，而目标（收藏页）其实
#   一开始就在屏幕上。提示词管不住的事得靠代码兜底（同 MAX_CONSECUTIVE_TYPE）。
#   上限设 3 而不是 1：给真正需要滚动的长列表留余量。
MAX_SLIDE_TOTAL = 3



# ------------------------------------------------------------
# TYPE 输入验证参数
# ------------------------------------------------------------
# 输入后等多久再回读（秒）。yadb 是异步触发粘贴，必须给系统反应时间。
TYPE_VERIFY_DELAY = 0.8



# 输入失败最多重试几次
TYPE_RETRY = 3



# ★ 方案 A（2026-09-30 加）：TYPE 前发现输入框没聚焦时，先点它一下，
#   然后等多久再输入（秒）。实测点击后 0.4~0.5 秒焦点就到位了。
TYPE_FOCUS_TAP_DELAY = 0.5



# ------------------------------------------------------------
# 连续 TYPE 拦截（实测新增）
# ------------------------------------------------------------
# 实测证据（2026-09-29 真机）：4B 模型在「点发送按钮点不中」之后，
# 会放弃点发送、改为**反复 TYPE 往输入框追加**，最后退化成 200 字的重复串
# （「你好啊呀呀呀…」），全部被真的输进输入框。
# 单靠提示词管不住，必须在代码层拦。
#
# 允许的最大连续 TYPE 次数（中间没有「点到输入框上」= 没有重新聚焦）。
#
# ★ 2026-10-04 改：清零判据从「点击 y 是否在屏幕下方」升级为「这次点击是否真的
#   压在输入框上」（读界面树判断，见 ui.point_hits_editable）。旧的「看屏幕下方」
#   是布局代理，换机型 / 横屏 / 浮窗会猜错，底部普通按钮也会被误当成重新聚焦输入区，
#   从而绕过本保护。判不清时不再瞎猜，而是停下并报告（见 StuckDetector.should_block）。
MAX_CONSECUTIVE_TYPE = 1



# yadb 校验用的 md5，与 GELab-Zero 项目保持一致（同一个二进制）
YADB_MD5 = "d64490deffddd4b711c74ea465397abb"



def _resolve_project_root() -> Path:
    """
    确定「项目根」—— 源码态是仓库根，安装态是用户目录。

    ★ 为什么不能直接写 `Path(__file__).parent.parent.parent`（2026-10-02 审计实测）：
      那个假设**只在源码态成立**（`<root>/src/phone_agent/config.py` 上溯三级 = `<root>`）。
      装进 site-packages 后上溯三级是 `<venv>/Lib`（Windows）或
      `<venv>/lib/python3.x`（Linux），后果有二：
        1. log/ 和 tmp/ 会往解释器目录旁边写 —— 可能没权限，而且不该写那儿；
        2. yadb 找不到 —— data-files 装到 `<venv>/bin`，不是 `<venv>/Lib/bin`。
      实测安装态下 `_PROJECT_ROOT` 与 data-files 的落点**不匹配**，所以必须区分。
    """
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent
    # 源码态：这个位置能看到 pyproject.toml，说明确实是仓库根
    if (candidate / "pyproject.toml").is_file():
        return candidate
    # 安装态：落到用户目录（保证可写，且不污染解释器目录）
    return Path.home() / ".phone_agent"


_PROJECT_ROOT = _resolve_project_root()

# yadb 二进制候选路径，按顺序找第一个存在的
YADB_CANDIDATES = [
    _PROJECT_ROOT / "bin" / "yadb",       # 源码态：项目根下的 bin/
    Path(sys.prefix) / "bin" / "yadb",    # 安装态：data-files 装到 sys.prefix/bin/
    Path(os.environ["YADB_PATH"]) if os.environ.get("YADB_PATH") else Path("__none__"),
]




# ============================================================
# 输出控制（2026-10-01 加）
# ============================================================
# 为什么要做：stdout 会被调用方（Pi / LLM）读进上下文。
# 实测每步约 200 字节，25 步 ≈ 5000 字节 ≈ 1500~2000 token ——
# 这是**唯一随步数增长**的上下文开销，值得压。
#
# ★ 设计原则（别搞反）：
#   * 过程日志  → 可静默（--quiet），但**始终落盘**
#   * 结论/错误 → **永远打屏**，不受 --quiet 影响。
#     调用方只读输出尾部，砍掉结论 = 砍掉它的判断依据
#     （2026-09-30 实测：Pi 看到退出码 3 就编了个「文件夹不存在」的理由）。

LOG_DIR = _PROJECT_ROOT / "log"

# 临时产物目录（完成截图、需人工介入的现场截图）
TMP_DIR = _PROJECT_ROOT / "tmp"

# 截图保留份数，超出的按修改时间删最旧
# 每张约 300 KB，不清理的话跑 100 次就是 30 MB
TMP_KEEP = 20


LOG_KEEP = 20        # 日志保留份数，超出的按修改时间删最旧




# adb 命令超时（秒）。全文只有 adb() 一处 subprocess 调用，改这里即全覆盖。
# 没有超时的话，设备中途掉线会让整个 agent 永久挂死 ——
# 上层（Pi 侧）只有 5 分钟超时，表现就是「没有任何输出地卡住」。
ADB_TIMEOUT = 30.0          # 普通命令


ADB_TIMEOUT_BINARY = 60.0   # 大流量（截图约 10 MB）




# 截图模式开关：
#   True  = RAW（`screencap` 不带 -p）—— 实测中位数 546 ms / 9.92 MB
#   False = PNG（`screencap -p`）      —— 实测中位数 2124 ms / 2.89 MB
# ★ 万一某次 USB 降速导致 RAW 反而更慢（实测偶发过一次 2810 ms），
#   把这里改成 False 即可切回，其余代码不用动。
USE_RAW_SCREENSHOT = True




# ------------------------------------------------------------
# 界面树读取（TYPE 的聚焦检查 + 回读验证共用）
# ------------------------------------------------------------
# 为什么抽出来（2026-09-30）：TYPE 现在要走「看有没有聚焦的输入框 → 没有就点它
# → 输入 → 再回读验证」，每步各 dump 一次会多花 1~2 秒。抽成公用件后，
# 「找输入框」和「回读验证」共用同一套解析逻辑，改动也只有一处。

# 界面树 dump 的落盘目录。用 /data/local/tmp 而不是 /sdcard：
#   /data/local/tmp 是 shell:shell 独占（实测 drwxrwx--x shell shell），
#   而 /storage/emulated/0 属 sdcard_rw 组 —— **别的 App 能写**。
#   放在公共目录里，理论上存在「别的 App 预置一份假界面树」被读到的可能。
_UI_DUMP_DIR = "/data/local/tmp"




# 模型调用重试次数。temperature=1.0 下重采样收益很高，
# 而旧实现是「一次坏输出 -> 整个任务退出码 1」，代价完全不成比例。
MODEL_RETRY = 3


MODEL_RETRY_BACKOFF = 1.0   # 秒；第 n 次重试前等 n * 该值




# 需要人工介入时的退出码。独立成一个码，是为了让调用方能**区分**
# 「失败了」和「等你操作」—— 混在 1/2 里，调用方会当成失败并自行编造原因。
EXIT_NEED_HUMAN = 4
