"""
测试用的假件与打桩工具 —— 不碰真机、不碰真模型、不往仓库/用户目录落盘。

设计要点（2026-10-05）：
  * 全部通过 `unittest.mock.patch` 替换 `phone_agent.runner` 里的**模块级名字**。
    runner 用 `from X import Y` 导入，这些名字就是 runner 模块的全局变量，
    所以**不需要改任何 src/ 下的代码**就能替换掉。
  * 别名白名单文件（`~/.phone_agent/apps.json`）会被重定向到临时目录，
    避免测试污染用户目录。
  * `time` 被换成 MagicMock —— `time.sleep` 变空操作，测试不真等。
"""

from __future__ import annotations

import io
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image as _PILImage

# 让 `import phone_agent` 可用（src 布局）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class FakeImage:
    """
    假截图：除了 runner 用到的 `.width/.height/.size/.save()`，
    还要支持 `crop/convert/resize` —— 界面指纹与黑屏检测会用到。

    为了不每个用例都分配 2.6M 像素，纯色底图在类级别共享（按 size+color 缓存）。
    """

    _SHARED = None
    _KEY = None

    def __init__(self, width: int = 1080, height: int = 2408,
                 color: tuple[int, int, int] = (32, 32, 32)) -> None:
        self.width = width
        self.height = height
        self.size = (width, height)
        self.color = color
        self.saved: list[str] = []

    def _pil(self):
        key = (self.width, self.height, self.color)
        if FakeImage._SHARED is None or FakeImage._KEY != key:
            FakeImage._SHARED = _PILImage.new("RGB", (self.width, self.height), self.color)
            FakeImage._KEY = key
        return FakeImage._SHARED

    def crop(self, box):
        return self._pil().crop(box)

    def convert(self, mode):
        return self._pil().convert(mode)

    def resize(self, size):
        return self._pil().resize(size)

    def save(self, path) -> None:
        self.saved.append(str(path))        # 只记录，不真写盘


class Handle:
    """打桩后交给测试用的句柄：记录调用与内部状态。"""

    def __init__(self) -> None:
        self.img = FakeImage()
        self.ask: object = None                 # 假的 ask_model 函数
        self.history_snapshots: list[list[dict]] = []   # 每次问模型时看到的历史
        self.exec_calls: list[dict] = []        # 真正执行过的动作
        self.report_calls: list[str] = []       # report_need_human 的原因
        self.launch_calls: list[str] = []       # launch_app 的入参

    @property
    def model_calls(self) -> int:
        """问了几次模型（= 走了几轮决策）。"""
        return len(self.history_snapshots)


def _make_ask(actions: list[dict], h: Handle):
    """按顺序吐动作；每次调用都把「当时看到的历史」快照下来，供断言用。"""
    state = {"n": 0}

    def _ask(client, model, view_img, task, history):
        h.history_snapshots.append([dict(x) for x in history])
        i = state["n"]
        state["n"] += 1
        if i < len(actions):
            return dict(actions[i])
        return {"action_type": "WAIT", "seconds": 1}   # 动作给完了就空转

    return _ask


@contextmanager
def alias_env():
    """把别名白名单文件重定向到临时目录 —— 让任何调用 load_app_aliases 的测试不污染 ~/.phone_agent。"""
    import phone_agent.apps as A

    tmp = Path(tempfile.mkdtemp(prefix="pa_alias_"))
    with mock.patch.multiple(A, APP_ALIASES_FILE=tmp / "apps.json",
                             _APP_ALIASES_CACHE=None), \
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            yield tmp / "apps.json"
        finally:
            for p in tmp.glob("*"):
                p.unlink()
            tmp.rmdir()


@contextmanager
def runner_env(actions, *, exec_fn=None, cur_pkg="com.android.settings",
               detect=None, texts=None, hits_editable=None, overrides=None):
    """
    把 runner 的「外部世界」全部打桩，yield 一个 Handle 供断言。

    :param actions:     假的 ask_model 依次返回的动作
    :param exec_fn:     假的 execute_action 策略 `(action) -> ExecResult`；
                        不传则默认「成功，且 COMPLETE 视为 finished」
    :param cur_pkg:     current_package 固定返回的包名
    :param detect:      detect_app_in_task 的返回值（None = 识别不出）
    :param overrides:   额外覆盖 {runner 属性名: 值}（优先级最高）
    """
    import phone_agent.apps as A
    import phone_agent.runner as R
    from phone_agent.actions import ExecResult

    h = Handle()
    h.ask = _make_ask(actions, h)
    tmp = Path(tempfile.mkdtemp(prefix="pa_test_"))

    if exec_fn is None:
        def exec_fn(action):                     # noqa: E306
            return ExecResult(
                ok=True,
                finished=str(action.get("action_type", "")).upper() == "COMPLETE")

    def _execute(action, size, device, has_yadb, dry_run, prefer_point=None):
        h.exec_calls.append(dict(action))
        return exec_fn(action)

    def _launch(name, device=None):
        h.launch_calls.append(str(name))
        return str(name)

    def _report(device, img, step, reason, **kwargs):
        h.report_calls.append(reason)

    patchers = {
        "ask_model": h.ask,
        "execute_action": _execute,
        "screenshot": lambda device=None: h.img,
        "resize_for_model": lambda im, w: im,
        "ensure_yadb": lambda device, verbose=True: True,
        "reset_to_home": lambda device, kill_package=None: None,
        "current_package": lambda device: cur_pkg,
        "detect_app_in_task": lambda task, device=None: detect,
        "launch_app": _launch,
        "visible_texts": lambda device=None: (texts or []),
        "point_hits_editable": lambda device, pt, margin_px=24: hits_editable,
        # 默认「读不到聚焦输入框」（None）—— 让「拦前回读放行」那条路径保持关闭，
        # 需要测它的用例自行 overrides
        "read_focused_text": lambda device=None: None,
        "report_need_human": _report,
        "prune_tmp": lambda *a, **k: None,
        "time": mock.MagicMock(),                 # time.sleep -> 空操作
    }
    patchers.update(overrides or {})

    # 顺手把 runner 的 print/info 吞掉 —— 否则测试输出全是步骤日志
    with mock.patch.multiple(R, **patchers), \
            mock.patch.multiple(A, APP_ALIASES_FILE=tmp / "apps.json",
                                _APP_ALIASES_CACHE=None), \
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            yield h
        finally:
            for p in tmp.glob("*"):
                p.unlink()
            tmp.rmdir()
