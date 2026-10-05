"""App 相关操作 —— 包名解析、启动、复位、任务里的 App 识别、中文输入。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .adb import AdbError, adb, keyevent
from .config import APP_ALIASES_FILE, YADB_CANDIDATES, YADB_MD5
from .output import info



def current_package(device: str | None = None) -> str:
    """
    当前前台 App 的包名；拿不到返回空串。

    用途：① 执行前复位时判断要不要 force-stop ② 完成时当作基线对比。
    """
    try:
        out = str(adb("shell", "dumpsys", "window", "displays", device=device))
    except AdbError:
        return ""
    # 优先 mCurrentFocus，退回 mFocusedApp
    for key in ("mCurrentFocus=", "mFocusedApp="):
        i = out.find(key)
        if i >= 0:
            seg = out[i + len(key):].split("\n")[0]
            # 形如 Window{abc u0 com.tencent.mobileqq/com.tencent.mobileqq.activity.SplashActivity}
            m = re.search(r"\b([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)/", seg)
            if m:
                return m.group(1)
    return ""




def list_packages(refresh: bool = False, device: str | None = None) -> list[str]:
    """已安装应用的包名列表（按设备缓存）。"""
    key = device or ""
    if not refresh and key in _PACKAGE_CACHE:
        return _PACKAGE_CACHE[key]
    out = str(adb("shell", "pm", "list", "packages", device=device))
    pkgs = [line.split(":", 1)[1].strip()
            for line in out.splitlines() if line.startswith("package:")]
    _PACKAGE_CACHE[key] = pkgs
    return pkgs




def resolve_package(name: str, device: str | None = None) -> str | None:
    """
    把应用名解析成包名。**只认精确匹配**（2026-10-05 收紧）：

      1. 别名表精确（"微信" -> com.tencent.mm、"浏览器" -> com.microsoft.emmx）
      2. 已安装包名精确（"com.tencent.mm" -> 它自己）

    其余一律返回 None —— 由调用方判失败，让模型改用**精确别名或包名**。

    ★ 为什么删掉模糊匹配（2026-10-05 用户反馈）：
      旧的「双向包含」会把「微信读书」解析成微信（com.tencent.mm）、
      「qq音乐」解析成 QQ —— 直接启动**错误的 App**，而且 Prompt 拦不住。
      安全边界必须落在代码里。（支持的 App 名单见 ~/.phone_agent/apps.json，
      要支持新 App 就往里加一条精确别名。）
    """
    key = (name or "").strip().lower()
    if not key:
        return None

    # ① 别名表精确（白名单来自 ~/.phone_agent/apps.json，见 load_app_aliases）
    aliases = load_app_aliases()
    if key in aliases:
        return aliases[key]

    # ② 已安装包名精确
    try:
        installed = list_packages(device=device)
    except AdbError:
        return None
    for pkg in installed:
        if key == pkg.lower():
            return pkg
    return None




def launch_app(name_or_package: str, device: str | None = None) -> str:
    """
    用 adb 直接启动应用，**绕开桌面图标识别**。

    ★ 为什么必须走这条路（2026-09-30 实测）：
      vivo 桌面（`com.bbk.launcher2`）是自绘 View，`uiautomator dump` 完全读不到
      App 图标 —— 整个桌面只有 45 个节点，且只剩一个搜索框。
      所以「在桌面上找图标再点击」在本机是死路：拿不到元素、模型也算不准图标坐标。
      而 `monkey -p <包名>` 只依赖包名，与桌面 UI 无关，实测稳定可用。

    ★ 为什么用 monkey 而不是 `am start`：
      monkey 只要包名，不需要知道 Activity 名 —— Activity 名随版本变，
      维护成本高且容易失效。

    :return: 实际启动的包名
    :raises AdbError: 解析不到包名，或 monkey 执行失败
    """
    pkg = resolve_package(name_or_package, device=device)
    if not pkg:
        raise AdbError(
            f"未知应用「{name_or_package}」—— 只接受**精确**的应用别名"
            f"（如「微信」「浏览器」）或**精确包名**（如 com.tencent.mm），不接受模糊名称"
        )
    adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1",
        device=device)
    return pkg




def detect_app_in_task(task: str, device: str | None = None) -> str | None:
    """
    从任务描述里猜出「要打开哪个 App」，返回包名；猜不到返回 None。

    用途：任务开始前就把目标 App 直接拉起来，让 agent 从 App 内部起步，
    而不是从桌面起步。这是「绕开桌面」最省事也最可靠的做法。

    ★ 为什么不能只靠「让模型自己输出 OPEN」：
      模型未必每次都会乖乖用 OPEN，而「任务里出现了哪个 App 名」是**确定性**的。
      先拉起，起点就一定是对的。

    ★ 匹配顺序：别名表（按键长从长到短）→ 已安装包名末段。
      按长度排序是为了让「系统设置」优先于「设置」，避免短键抢先命中。

    ⚠️ 本函数是**模糊猜测**（子串匹配），返回值**不可直接拿去启动**（2026-10-05）：
      任务「打开微信读书」里含别名「微信」→ 会被猜成微信 → 启动**错误的 App**。
      调用方（runner）必须先过 `_is_pure_open_task()` 这道「纯打开」确认才允许启动；
      其他情况交给模型自己 OPEN。
    """
    t = (task or "").lower()
    if not t:
        return None

    aliases = load_app_aliases()
    for alias in sorted(aliases, key=len, reverse=True):
        if alias and alias in t:
            return aliases[alias]

    try:
        installed = list_packages(device=device)
    except AdbError:
        return None
    # 任务里直接写了完整包名（如「打开 com.tencent.mm」）
    for pkg in sorted(installed, key=len, reverse=True):
        if pkg.lower() in t:
            return pkg
    # 末段至少 4 个字符才拿来匹配，避免 "mm"、"xhs" 这类短片段误命中
    for pkg in sorted(installed, key=len, reverse=True):
        seg = pkg.rsplit(".", 1)[-1].lower()
        if len(seg) >= 4 and seg in t:
            return pkg
    return None




def reset_to_home(device: str | None, kill_package: str | None = None,
                  verbose: bool = True) -> None:
    """
    把手机恢复到「已知的干净起点」，让任务的输入可复现。

    ★ 为什么必须做（2026-09-29 实测踩坑）：
    跑完一个任务后 App 还停在那儿，下一轮任务的起点就是「上一轮的终点」。
    实测第 2 轮从 QQ 聊天界面起跑，模型第 1 步看到「已经在聊天界面、消息也发过了」
    就直接输出 COMPLETE —— 1 步假成功。**这不是模型的问题，是起跑状态没复位。**

    默认只按 HOME（安全）。force-stop 会杀掉后台，
    对 QQ/微信这类需要常驻收消息的 App 影响大，所以**只在你显式指定时才做**。

    :param kill_package: 要强制停止的包名；None 表示不强杀任何 App
    """
    before = current_package(device)

    if kill_package:
        if verbose:
            info(f"[复位] 强制停止 {kill_package}（当前前台：{before or '未知'}）")
        try:
            adb("shell", "am", "force-stop", kill_package, device=device)
            time.sleep(1.0)
        except AdbError as exc:
            if verbose:
                print(f"[警告] 强杀 {kill_package} 失败：{exc}")

    # 连按两次 HOME：第一次可能只是收起键盘/退出弹窗，第二次才真回桌面
    keyevent(3, device)
    time.sleep(0.6)
    keyevent(3, device)
    time.sleep(1.0)

    after = current_package(device)
    if verbose:
        info(f"[复位] 回到桌面（前台：{after or '未知'}）")
        if not _is_home(after):
            print("[警告] 复位后似乎不在桌面，可能被某 App 拦截了返回；继续执行但结果可能不准")




def _is_home(pkg: str) -> bool:
    """这个包名看起来是不是桌面。"""
    if not pkg:
        return False
    low = pkg.lower()
    return any(h in low for h in HOME_HINTS)




# ============================================================
# yadb：中文输入与精确手势
# ============================================================

def find_yadb() -> Path | None:
    """在候选路径里找 yadb 二进制。"""
    for p in YADB_CANDIDATES:
        if p.is_file():
            return p
    return None




def ensure_yadb(device: str | None, verbose: bool = True) -> bool:
    """
    确保手机 /data/local/tmp/ 下有 yadb。

    先比对 md5，不一致才推送（省掉每次启动都传 70KB）。
    :return: True 表示可用，False 表示本地找不到 yadb 文件
    """
    local = find_yadb()
    if not local:
        if verbose:
            print("[警告] 本地找不到 yadb，中文输入和精确手势将不可用")
        return False

    try:
        out = str(adb("shell", "md5sum", "/data/local/tmp/yadb", device=device))
    except AdbError:
        out = ""

    if YADB_MD5 in out:
        if verbose:
            info("[OK] 手机端 yadb 已就绪")
        return True

    if verbose:
        info("[..] 正在向手机推送 yadb ...")
    adb("push", str(local), "/data/local/tmp/", device=device)
    if verbose:
        info("[OK] yadb 推送完成")
    return True




def input_text(text: str, device: str | None = None, has_yadb: bool = True) -> None:
    """
    在当前焦点处输入文字。

    纯 ASCII 且无空格走 adb 原生 input text（快）；
    含中文、空格或特殊字符走 yadb —— 原生 input text 对中文完全无能为力。
    """
    if text.isascii() and " " not in text and text.isprintable():
        # 原生 input text 对 shell 元字符敏感，这里只走安全字符集
        if re.fullmatch(r"[A-Za-z0-9._@\-]+", text):
            adb("shell", "input", "text", text, device=device)
            return

    if not has_yadb:
        raise AdbError(f"需要输入「{text}」，但 yadb 不可用，无法输入非 ASCII 文本")

    adb("shell",
        "app_process",
        "-Djava.class.path=/data/local/tmp/yadb",
        "/data/local/tmp",
        "com.ysbing.yadb.Main",
        "-keyboard", text,
        device=device)




# ============================================================
# 应用启动（绕开桌面图标识别）
# ============================================================

# ★ 内置默认白名单（种子）。真正的白名单在 ~/.phone_agent/apps.json：
#   首次运行会用下面这份生成该文件，之后**只以文件为准**（增删应用改文件即可）。
#   这里只作「文件缺失/损坏」时的兜底，正常运行时不再直接读它。
_DEFAULT_APP_PACKAGES: dict[str, str] = {
    # ---- 中文名 ----
    "微信": "com.tencent.mm",
    "qq": "com.tencent.mobileqq",
    "抖音": "com.ss.android.ugc.aweme",
    "淘宝": "com.taobao.taobao",
    "支付宝": "com.eg.android.AlipayGphone",
    "京东": "com.jingdong.app.mall",
    "拼多多": "com.xunmeng.pinduoduo",
    "微博": "com.sina.weibo",
    "小红书": "com.xingin.xhs",
    "高德地图": "com.autonavi.minimap",
    "百度网盘": "com.baidu.netdisk",
    "王者荣耀": "com.tencent.tmgp.sgame",
    "多邻国": "com.duolingo",
    "豆包": "com.larus.nova",
    "扇贝单词": "com.jiongji.andriod.card",
    "轻小说文库": "org.mewx.wenku8",
    "设置": "com.android.settings",
    "系统设置": "com.android.settings",
    # ---- 英文名 / 别名 ----
    "wechat": "com.tencent.mm",
    "douyin": "com.ss.android.ugc.aweme",
    "tiktok": "com.ss.android.ugc.aweme",
    "taobao": "com.taobao.taobao",
    "alipay": "com.eg.android.AlipayGphone",
    "weibo": "com.sina.weibo",
    "gaode": "com.autonavi.minimap",
    "amap": "com.autonavi.minimap",
    "spotify": "com.spotify.music",
    "chatgpt": "com.openai.chatgpt",
    "edge": "com.microsoft.emmx",
    "浏览器": "com.microsoft.emmx",
    "outlook": "com.microsoft.office.outlook",
    "twitter": "com.twitter.android",
    "termux": "com.termux",
    "pixiv": "jp.pxv.android",
    "settings": "com.android.settings",
}


# 白名单缓存：避免每次解析都读盘
_APP_ALIASES_CACHE: dict[str, str] | None = None


def load_app_aliases(refresh: bool = False) -> dict[str, str]:
    """
    读取应用白名单（别名 -> 包名）。

    来源：`~/.phone_agent/apps.json`（路径见 config.APP_ALIASES_FILE）。
      * 文件不存在 -> 用内置默认 `_DEFAULT_APP_PACKAGES` 生成一份，方便直接编辑；
      * 文件存在   -> **只以文件为准**（增删应用改它即可，不用碰源码）；
      * 读不到/格式坏 -> 退回内置默认，保证不崩。

    ★ 为什么外置成文件（2026-10-05）：内置在源码里的白名单，加个 App 得改代码、
      重装才生效。外置后用户/调用方 AI 直接改 json 就行。

    :param refresh: 强制重新读盘（忽略缓存）
    """
    global _APP_ALIASES_CACHE
    if _APP_ALIASES_CACHE is not None and not refresh:
        return _APP_ALIASES_CACHE

    path = APP_ALIASES_FILE
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_DEFAULT_APP_PACKAGES, ensure_ascii=False, indent=2),
                encoding="utf-8")
            info(f"[应用白名单] 首次运行，已生成默认名单：{path}（以后加应用改它即可）")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("内容不是 {名称: 包名} 的字典")
        cleaned = {str(k).strip().lower(): str(v).strip()
                   for k, v in data.items() if str(k).strip() and str(v).strip()}
        _APP_ALIASES_CACHE = cleaned
    except Exception as exc:
        info(f"[应用白名单] 读 {path} 失败（{exc}），改用内置默认名单")
        _APP_ALIASES_CACHE = {k.lower(): v for k, v in _DEFAULT_APP_PACKAGES.items()}
    return _APP_ALIASES_CACHE



# 已安装包名缓存。★ 必须按设备序列号分开（2026-09-30 审计）：
# 旧实现是单个全局值、命中缓存后**忽略 device 参数**，接第二台手机时
# 会拿 A 机的包名列表去解析 B 机的应用。这里与 _SCREEN_SIZE_CACHE 统一口径。
_PACKAGE_CACHE: dict[str, list[str]] = {}




# 各家的桌面/启动器包名特征。放宽匹配，别把「回到桌面」误判成失败。
HOME_HINTS = ("launcher", "home", "desk", "bbk.launcher", "miui.home",
              "launcher3", "trebuchet", "systemui")
