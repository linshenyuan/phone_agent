"""输出控制 —— 过程日志可静默、可落盘；结论/错误永远打屏。"""

from __future__ import annotations

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


def setup_log(log_file: str | None = None, no_log: bool = False,
              quiet: bool = False) -> None:
    """
    装配日志：设置静默开关 + 打开日志文件 + 把 stdout/stderr 接上 Tee。

    ★ 为什么把这段从 main() 搬进来（2026-10-02 拆分时）：
      QUIET 和 _log_handle 是**本模块的**全局状态，main() 里写
      `global QUIET` 改的是它自己模块的变量，碰不到这里 —— 静默会失效。
      与其让调用方去改 `output.QUIET`，不如把整套装配封成一个函数。
    """
    global QUIET, _log_handle
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

    sys.stdout = _Tee(sys.stdout, _log_handle)
    sys.stderr = _Tee(sys.stderr, _log_handle)
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
