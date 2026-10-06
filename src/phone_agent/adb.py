"""ADB 基础封装 —— 命令执行、设备选择、截图、点击、滑动、按键。"""

from __future__ import annotations

import subprocess

from .config import ADB_TIMEOUT, ADB_TIMEOUT_BINARY, USE_RAW_SCREENSHOT
from .deps import Image
from .output import info
import io
import re
import shlex
import shutil
import sys



# ============================================================
# ADB 基础封装
# ============================================================

class AdbError(RuntimeError):
    """adb 命令执行失败。"""




def _adb_bin() -> str:
    """定位 adb 可执行文件；PATH 里没有就抛 **AdbError**。

    ★ 为什么是抛异常而不是 sys.exit（2026-10-06 修）：
      调用方写的是 `except AdbError`（apps.resolve_package、runner 共 5 处），
      而 sys.exit() 抛的是 **SystemExit** —— 它接不住，异常会一路逃出调用栈，
      让整个进程猝死且**不留 [结论] 行**。
      最典型的受害者是 resolve_package：它本想「adb 有问题就返回 None」，
      却因为异常类型不对，把「查不到包名」升级成了「进程直接退出」。
      抛 AdbError 后，既有的降级路径才真正生效。

      注：pick_device 里的 sys.exit 是**有意保留**的 —— 那是命令行参数错误
      （设备没指定 / 不在线），由 CLI 入口调用，且有测试断言 SystemExit。
    """
    exe = shutil.which("adb")
    if not exe:
        raise AdbError(
            "找不到 adb。请确认已安装 platform-tools 并加入 PATH。\n"
            "下载：https://developer.android.com/tools/releases/platform-tools"
        )
    return exe




def adb(*args: str, device: str | None = None, binary: bool = False,
        timeout: float | None = None) -> bytes | str:
    """
    执行一条 adb 命令。

    :param args: adb 的子命令与参数，例如 ("shell", "input", "tap", "100", "200")
    :param device: 设备序列号；多设备时必须指定
    :param binary: True 时返回 bytes（截图用），False 时返回解码后的文本
    :param timeout: 超时秒数；None 时按 binary 自动选默认值
    :return: 命令输出

    ★★ 为什么必须对 shell/exec-out 的参数做 shlex.quote（2026-09-30 实测）：
      adb 收到多个 argv 后**只用空格拼接、不做任何转义**，整串交给手机端 shell
      解析。实测 `adb shell echo 'a;id'` 会真的执行 `id`（返回 uid=2000(shell)），
      `$(...)`、反引号同样被执行 —— 而 shell 用户有 sdcard_rw，可读写删 /sdcard。

      ★ 而且**不需要攻击**，日常文字就会被静默篡改（同一批实测）：
          `价格$100`  -> `价格00`      （变量展开）
          `a&b`       -> `a`           （后台执行截断）
          `备注：A;B`  -> `备注：A`      （分号截断）
          `echo 5>3`  -> 空            （重定向）
          `(括号)`     -> 空            （子 shell 语法错误）

      修法：拼成**一条**已引用的字符串，让设备端 shell 把每个参数当字面量。
      放在这里而不是各调用点 —— 这是唯一出口，一处覆盖全部调用点，
      以后新增调用点自动安全。

    ⚠️ 边界（2026-10-05，别把话说满）：shlex.quote 挡的是 **host 侧
      传给设备端 shell** 的注入。它**挡不住**设备端 `input` 命令自己的文本处理 ——
      例如 `input text` 会把 `%s` 替换成空格。当前所有经 `input` 的文本都被上层
      白名单/字符集限制挡住，属**潜伏坑**，不是已修的洞。
    """
    cmd = [_adb_bin()]
    if device:
        cmd += ["-s", device]

    if args and args[0] in ("shell", "exec-out", "exec-in"):
        # 只对 shell 系子命令引用参数；push / devices 等非 shell 子命令保持原样
        cmd.append(args[0])
        cmd.append(" ".join(shlex.quote(a) for a in args[1:]))
    else:
        cmd += list(args)

    if timeout is None:
        timeout = ADB_TIMEOUT_BINARY if binary else ADB_TIMEOUT

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AdbError(
            f"adb {' '.join(args)} 超时（{timeout:g}s）—— 设备可能已断开或驱动异常"
        )

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise AdbError(f"adb {' '.join(args)} 失败：{err}")

    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")




def list_devices() -> list[str]:
    """列出已授权且在线的设备序列号。"""
    out = str(adb("devices"))
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        # 只认状态为 device 的；unauthorized / offline 都跳过
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices




def pick_device(explicit: str | None) -> str | None:
    """
    决定用哪台设备。

    :param explicit: 命令行显式传入的 -s 值
    :return: 设备序列号；只有一台时返回它，没有设备时返回 None
    """
    devices = list_devices()
    if not devices:
        return None
    if explicit:
        if explicit not in devices:
            sys.exit(f"指定的设备 {explicit} 不在线。当前在线：{', '.join(devices)}")
        return explicit
    # ★ 多设备时必须显式指定（2026-10-05 修）：
    #   旧实现直接 `return devices[0]`。对一个会**发消息**的工具来说，
    #   「静默操作了错误的手机」是最糟的结果 —— 宁可报错让用户补 --device。
    if len(devices) > 1:
        sys.exit(f"检测到 {len(devices)} 台在线设备，请用 --device 指定要操作哪一台："
                 f"{', '.join(devices)}")
    return devices[0]




def screen_size(device: str | None = None, refresh: bool = False) -> tuple[int, int]:
    """
    当前屏幕分辨率。

    优先取 Override size —— screencap 输出的就是**实际显示尺寸**，
    用户用 `wm size` 改过分辨率时，Physical size 会与实际输出不符。

    带缓存：`wm size` 是一次 adb 往返（实测约 50 ms），
    每步截图都重查会白吃掉 RAW 模式省下的一部分收益。
    """
    key = device or ""
    if not refresh and key in _SCREEN_SIZE_CACHE:
        return _SCREEN_SIZE_CACHE[key]

    size: tuple[int, int] = (1080, 2408)
    try:
        out = str(adb("shell", "wm", "size", device=device))
    except AdbError:
        return size

    override: tuple[int, int] | None = None
    physical: tuple[int, int] | None = None
    for m in re.finditer(r"(Override|Physical)\s+size:\s*(\d+)x(\d+)", out):
        s = (int(m.group(2)), int(m.group(3)))
        if m.group(1) == "Override":
            override = s
        else:
            physical = s

    size = override or physical or size
    _SCREEN_SIZE_CACHE[key] = size
    return size




def _open_png(png: bytes) -> Image.Image:
    """
    把 PNG 字节解析成 RGB 图片；数据损坏时抛 **AdbError**（不是裸 OSError）。

    ★ 为什么必须包一层（2026-10-05）：`Image.open` 遇到空 / 垃圾 /
      截断数据会抛 `UnidentifiedImageError` —— 它**是 OSError 但不是 AdbError**。
      而 runner 的截图分支只 `except AdbError`，于是这类异常会**逃出 run()**、
      以未捕获 traceback 崩溃，本该走的「连续黑屏 -> 请人工解锁」流程也失效。
      统一包成 AdbError 后，上层就能正常收尾并留下 `[结论]` 行。
    """
    try:
        return Image.open(io.BytesIO(png)).convert("RGB")
    except OSError as exc:
        raise AdbError(
            f"截图数据无法解析为图片（{type(exc).__name__}: {exc}）"
            f"—— 设备可能返回了空 / 损坏的截图"
        ) from exc


def screenshot(device: str | None = None) -> Image.Image:
    """
    抓取手机当前屏幕。

    ★ 2026-09-30 实测：改用 RAW 模式（不带 -p）比 PNG 快约 1.6 秒。

        PNG  (`screencap -p`) : 2124 ms / 2.89 MB
        RAW  (`screencap`)    :  546 ms / 9.92 MB
        纯传输基准 (2.83 MB)  :  577 ms

    RAW 传的数据量大 3.4 倍，反而快 4 倍 —— 瓶颈是**手机侧的 PNG 压缩**（约 1.6 秒），
    不是带宽。多传 7 MB 的开销远小于省下的压缩时间。

    ★★ RAW 数据前 16 字节是 header（新版 Android 的 screencap 会带）：
        [0:4] 宽   [4:8] 高   [8:12] 格式   [12:16] 色彩空间
        全是 little-endian uint32
      所以**不需要**再查 `wm size` —— 直接从 header 读尺寸，还能自动跟上旋转与分辨率变更。
      实测：header 报 1080x2408，16 + 1080*2408*4 = 10,402,576 = 实际长度，完全吻合。
      ⚠️ 最初没识别这个 header，长度校验差了 16 字节 → 静默退回 PNG → RAW 的收益全丢，
      表面看只是"变慢了"，极难发现。**这类"校验不通过就静默降级"的分支一定要能观测到。**

    三级兜底：① header 解析 → ② 裸像素 + `wm size` → ③ PNG。

    注意：exec-out 是 binary-safe 的，**绝不能**做换行转换 ——
    PNG 魔数本身就含 0d 0a（\\x89PNG\\r\\n\\x1a\\n），无条件把 \\r\\n 换成 \\n
    会直接破坏文件头，PIL 报 UnidentifiedImageError。
    （实测本机 adb：输出里 \\r\\n 仅 46 次、\\n 达 13201 次 —— 没有换行污染。）
    """
    if not USE_RAW_SCREENSHOT:
        png = adb("exec-out", "screencap", "-p", device=device, binary=True)
        assert isinstance(png, bytes)
        return _open_png(png)

    raw = adb("exec-out", "screencap", device=device, binary=True)
    assert isinstance(raw, bytes)

    # ① 带 header 的 RAW：新版 16 字节（宽/高/格式/色彩空间），
    #    旧版 Android 7/8 只有 12 字节（宽/高/格式，AOSP 早期省略色彩空间）。
    #    ★ 12 字节分支（2026-10-05）：旧实现只认 16 字节 → 老机型
    #      恒定静默退回 PNG，每帧多约 1.6 秒且**没有任何日志**（正是本项目自己
    #      警告过的「静默降级必须可观测」）。现在两条都认，且降级路径会打日志。
    if len(raw) >= 12:
        hw = int.from_bytes(raw[0:4], "little")
        hh = int.from_bytes(raw[4:8], "little")
        if 0 < hw < 100000 and 0 < hh < 100000:
            if len(raw) == hw * hh * 4 + 16:
                return Image.frombytes("RGBA", (hw, hh), raw[16:]).convert("RGB")
            if len(raw) == hw * hh * 4 + 12:
                info("[截图] 识别到 12 字节 header（旧版 Android 7/8），按 12 字节偏移解析")
                return Image.frombytes("RGBA", (hw, hh), raw[12:]).convert("RGB")

    # ② 无 header 的裸像素
    w, h = screen_size(device)
    if len(raw) != w * h * 4:
        if len(raw) == h * w * 4:
            w, h = h, w  # 屏幕旋转：只是宽高互换，字节数不变
        else:
            w, h = screen_size(device, refresh=True)  # 可能改过分辨率，重查一次
            if len(raw) != w * h * 4 and len(raw) == h * w * 4:
                w, h = h, w
    if len(raw) == w * h * 4:
        return Image.frombytes("RGBA", (w, h), raw).convert("RGB")

    # ③ 尺寸始终对不上 -> 退回 PNG
    #    ★ 打日志（2026-10-05）：静默降级是「变慢了却查不出原因」的典型
    #      —— 当初漏认 header 就是这么被坑的。这里明确记下长度与推算尺寸，便于排查。
    info(f"[截图] RAW 尺寸对不上（收到 {len(raw)} 字节，推算 {w}x{h}*4={w * h * 4}）"
         f"—— 退回 PNG（慢约 1.6 秒）")
    png = adb("exec-out", "screencap", "-p", device=device, binary=True)
    assert isinstance(png, bytes)
    return _open_png(png)




def tap(x: int, y: int, device: str | None = None) -> None:
    """在屏幕坐标 (x, y) 处点击一次。"""
    adb("shell", "input", "tap", str(int(x)), str(int(y)), device=device)




def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
          device: str | None = None) -> None:
    """从 (x1,y1) 滑动到 (x2,y2)，duration_ms 是按住时长。"""
    adb("shell", "input", "swipe",
        str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)),
        device=device)




def keyevent(code: int, device: str | None = None) -> None:
    """发送按键事件。4=返回 3=Home 26=电源 66=回车。"""
    adb("shell", "input", "keyevent", str(code), device=device)



# 屏幕尺寸缓存：key 是设备序列号（空串代表默认设备）
_SCREEN_SIZE_CACHE: dict[str, tuple[int, int]] = {}
