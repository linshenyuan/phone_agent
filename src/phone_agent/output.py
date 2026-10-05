"""输出控制 —— 过程日志可静默、可落盘；结论/错误永远打屏。"""

from __future__ import annotations

import atexit
import sys
import time
from pathlib import Path
from typing import Any

from .config import _PROJECT_ROOT, LOG_DIR, LOG_KEEP, TMP_DIR, TMP_KEEP



class _Tee:
    """
    把 stdout 同时写进屏幕和日志文件。

    ★ 为什么需要它：结论行是用 print 打的（不是 info）。如果只让 info 写文件，
      日志里就会缺结论 —— 那不叫「完整日志」。包住 sys.stdout 之后，
      所有输出（含结论、含报错）都自动进文件。
    """

    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, s: str) -> None:
        for st in self.streams:
            st.write(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()




def _open_log() -> Any:
    """开一个带时间戳的日志文件，并清理超量的旧日志。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle = open(path, "w", encoding="utf-8")

    # 清理：按修改时间倒序，只留最近 LOG_KEEP 份。失败不影响主流程。
    try:
        old = sorted(LOG_DIR.glob("run_*.log"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[LOG_KEEP:]:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return handle




def info(*args: Any, **kwargs: Any) -> None:
    """
    过程日志：--quiet 时只进日志文件、不打屏；正常时两边都有。

    ★ 只用于「排查用的中间信息」（步数、屏幕尺寸、模型决策 JSON 之类）。
      结论、错误、警告一律仍用 print —— 它们是调用方唯一的判断依据。
    """
    if QUIET:
        if _log_handle is not None:
            print(*args, file=_log_handle, **kwargs)     # 绕过 Tee，只落盘
    else:
        print(*args, **kwargs)                            # 经 Tee → 屏幕 + 文件



QUIET = False


_log_handle: Any = None


# 装配前的原始 stdout/stderr —— teardown 时还原用（只还原、不关闭，它们是别人的）
_ORIG_STDOUT: Any = None
_ORIG_STDERR: Any = None


# atexit 只注册一次
_atexit_registered = False


def teardown_log() -> None:
    """
    收尾：还原 stdout/stderr，并关闭自己开的日志文件。**可重复调用**（第二次起是空操作）。

    ★ 为什么要有它（2026-10-05）：setup_log 换了 sys.stdout/stderr，原来却没有任何收尾。
      CLI 一次性跑时进程退出、解释器会兜底关文件，问题不大；但作为 library 调用、
      或嵌进长驻进程时，main() 返回后 stdout 还指着已关闭的日志文件 → 后续 print 出错。
      所以必须有明确的 teardown，并在 main() 的 finally 里调用。

    ★ 顺序：先还原 stdout/stderr，再关文件 —— 反了的话 Tee 还指着已关闭的文件，
      收尾期间的任何 print 都会报错。
    ★ 只动自己创建的东西：原始流只还原、不 close。
    """
    global _ORIG_STDOUT, _ORIG_STDERR, _log_handle, QUIET
    if _ORIG_STDOUT is not None:
        sys.stdout = _ORIG_STDOUT
        _ORIG_STDOUT = None
    if _ORIG_STDERR is not None:
        sys.stderr = _ORIG_STDERR
        _ORIG_STDERR = None
    if _log_handle is not None:
        try:
            _log_handle.flush()
            _log_handle.close()
        except OSError:
            pass
        _log_handle = None
    QUIET = False


def setup_log(log_file: str | None = None, no_log: bool = False,
              quiet: bool = False) -> None:
    """
    装配日志：设置静默开关 + 打开日志文件 + 把 stdout/stderr 接上 Tee。

    ★ 幂等（2026-10-05）：重复调用会先 teardown 上一次的装配，
      不会套两层 Tee、也不会泄漏上一个 handle。
    ★ 兜底：注册一次 `atexit.teardown_log`，万一调用方忘了收尾也能还原。

    ★ 为什么把这段从 main() 搬进来（2026-10-02 拆分时）：
      QUIET 和 _log_handle 是**本模块的**全局状态，main() 里写
      `global QUIET` 改的是它自己模块的变量，碰不到这里 —— 静默会失效。
      与其让调用方去改 `output.QUIET`，不如把整套装配封成一个函数。
    """
    global QUIET, _log_handle, _ORIG_STDOUT, _ORIG_STDERR, _atexit_registered
    # ★ 幂等：先拆掉上一次的装配（没有则空操作）
    teardown_log()
    QUIET = quiet
    if no_log:
        return

    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            # 相对路径解析到**项目根**（别跟着调用方 cwd 跑）
            log_path = _PROJECT_ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _log_handle = open(log_path, "w", encoding="utf-8")
    else:
        _log_handle = _open_log()

    # 记下原始流（teardown 时还原用），再把 stdout/stderr 接上 Tee
    _ORIG_STDOUT = sys.stdout
    _ORIG_STDERR = sys.stderr
    sys.stdout = _Tee(sys.stdout, _log_handle)
    sys.stderr = _Tee(sys.stderr, _log_handle)

    # 兜底收尾：异常 / 直接退出时也能还原。只注册一次。
    if not _atexit_registered:
        atexit.register(teardown_log)
        _atexit_registered = True

    print(f"[日志] 完整日志：{_log_handle.name}")


def prune_tmp(keep: int | None = None) -> None:
    """
    清理 tmp/ 里的旧截图，只留最近 N 张。

    ★ 为什么需要（2026-10-02 加）：
      截图是「判断假成功」的唯一依据，所以必须存 —— 但它**只进不出**：
      每跑一次任务留一张（约 300 KB），跑 100 次就是 30 MB，而且永不删除。
      这里按修改时间保留最近 TMP_KEEP 张，和 log/ 的处理方式对称。

    ★ 为什么清理失败要吞掉异常：这是收尾动作，不该因为「删不掉一张旧图」
      就把整个任务判成失败。

    :param keep: 保留份数；不传则用 config.TMP_KEEP
    """
    k = TMP_KEEP if keep is None else keep
    try:
        shots = sorted(TMP_DIR.glob("*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in shots[k:]:
            p.unlink(missing_ok=True)
    except OSError:
        pass
