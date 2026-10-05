"""
runner 主循环的**控制流测试**（行为冻结，2026-10-05）。

为什么要它：2026-09-29 踩过一个坑 —— 单测只模拟「动作序列」、没模拟
「哪一步被跳过（continue）、跳过的步有没有回写状态」，结果两次错误互相抵消，
单测全绿而真机照样错。所以这里**必须走完整 run() 控制流**，断言
「问了几次模型 / 执行了几次 / 历史里留了什么 / 退出码是多少」。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 `import fakes` 可用

import fakes                                                  # noqa: E402
from fakes import runner_env                                  # noqa: E402

from phone_agent import runner as R                           # noqa: E402
from phone_agent.actions import ExecResult                    # noqa: E402


def CLICK(x=500, y=800):
    return {"action_type": "CLICK", "point": (x, y)}


def TYPE(text="你好"):
    return {"action_type": "TYPE", "value": text}


def OPEN(app="网易云音乐"):
    return {"action_type": "OPEN", "app": app}


COMPLETE = {"action_type": "COMPLETE"}


def run_task(task, max_steps, **kw):
    """用最小的必需参数调 run()；默认关掉自动启动（要测它时单独开）。"""
    return R.run(task=task, device=None, client=object(), model="m",
                 view_width=720, max_steps=max_steps, step_delay=0.0,
                 auto_launch=False, **kw)


class RunnerFlow(unittest.TestCase):
    """主循环控制流。"""

    def test_01_正常完成_退出码0(self):
        with runner_env([CLICK(), CLICK(), COMPLETE]) as h:
            rc = run_task("打开设置", max_steps=5)
        self.assertEqual(rc, 0)
        self.assertEqual(len(h.exec_calls), 3)
        self.assertEqual(h.model_calls, 3)

    def test_02_执行失败要写进历史(self):
        # 关键：失败的动作必须带 failed/error 进历史，否则模型看不到失败会原样重试
        with runner_env([CLICK(), CLICK()],
                        exec_fn=lambda a: ExecResult(ok=False, note="点击失败：xxx")) as h:
            rc = run_task("打开设置", max_steps=2)
        self.assertEqual(rc, 1)                       # 步数耗尽
        last = h.history_snapshots[-1]
        self.assertTrue(any(x.get("failed") for x in last), f"历史里没有 failed 标记：{last}")
        self.assertTrue(any(x.get("error") for x in last), f"历史里没有 error：{last}")

    def test_03_连续TYPE被拦后仍进入下一轮决策(self):
        # ★ 2026-09-29 那个坑的针对性用例：被拦那步「不执行」，但必须「继续问模型」
        with runner_env([CLICK(), TYPE(), TYPE(), COMPLETE]) as h:
            rc = run_task("打开设置", max_steps=4)
        self.assertEqual(h.model_calls, 4, "被拦后没有进入下一轮模型决策")
        self.assertEqual(len(h.exec_calls), 3, "被拦的那一步不该执行")
        last = h.history_snapshots[-1]
        self.assertTrue(any(x.get("action_type") == "BLOCKED_TYPE" for x in last),
                        f"历史里没有 BLOCKED_TYPE：{last}")
        self.assertEqual(rc, 0)

    def test_04_可疑完成_退出码3(self):
        # 发文字任务却一次 TYPE 都没有 + 只 1 步完成 -> 可疑完成
        with runner_env([COMPLETE]) as h:
            rc = run_task("给张三发消息", max_steps=5)
        self.assertEqual(rc, 3)
        self.assertEqual(h.model_calls, 1)

    def test_05_OPEN连续失败_退出码4(self):
        with runner_env([OPEN(), OPEN(), OPEN()],
                        exec_fn=lambda a: ExecResult(ok=False, note="未知应用")) as h:
            rc = run_task("打开设置", max_steps=5)
        self.assertEqual(rc, 4)
        self.assertTrue(h.report_calls, "停下问用户时应调用 report_need_human")

    def test_06_TYPE失败_退出码2(self):
        with runner_env([TYPE()],
                        exec_fn=lambda a: ExecResult(ok=False, note="输入失败")) as h:
            rc = run_task("给张三发消息", max_steps=5)
        self.assertEqual(rc, 2)

    def test_07_需要人工介入_退出码4(self):
        with runner_env([TYPE("secret")],
                        exec_fn=lambda a: ExecResult(ok=True, need_human=True,
                                                     human_reason="密码框")) as h:
            rc = run_task("给张三发消息", max_steps=5)
        self.assertEqual(rc, 4)
        self.assertTrue(h.report_calls)

    def test_08_步数耗尽_退出码1(self):
        with runner_env([CLICK()]) as h:
            rc = run_task("打开设置", max_steps=3)
        self.assertEqual(rc, 1)
        self.assertEqual(len(h.exec_calls), 3)

    def test_09_纯打开任务短路_退出码0(self):
        # 任务点名了 App 且是「纯打开」-> 直接拉起并收工，不烧模型
        with runner_env([], detect="com.android.settings",
                        cur_pkg="com.android.settings") as h:
            rc = R.run(task="打开设置", device=None, client=object(), model="m",
                       view_width=720, max_steps=5, step_delay=0.0, auto_launch=True)
        self.assertEqual(rc, 0)
        self.assertEqual(h.launch_calls, ["com.android.settings"])
        self.assertEqual(h.model_calls, 0, "纯打开短路不该调用模型")

    def test_10_同一点反复点击_判卡死退出码2(self):
        with runner_env([CLICK(500, 800)] * 6) as h:
            rc = run_task("打开设置", max_steps=10)
        self.assertEqual(rc, 2)
        self.assertLess(len(h.exec_calls), 10, "卡死应在步数耗尽前就终止")


class 拦前回读输入框(unittest.TestCase):
    """★ Claude 审查 10.3：连续 TYPE 被拦前先回读输入框，空则放行。"""

    def test_回读为空则放行照常执行(self):
        # CLICK → TYPE → TYPE：第 3 步本该被拦；回读输入框为空 -> 应放行
        actions = [CLICK(), TYPE("你好"), TYPE("世界"), COMPLETE]
        with runner_env(actions,
                        overrides={"read_focused_text": lambda device=None: ""}) as h:
            rc = run_task("打开设置", max_steps=4)
        self.assertEqual(len(h.exec_calls), 4, "回读为空时应放行")
        self.assertEqual(rc, 0)

    def test_读不到输入框时仍保守拦下(self):
        actions = [CLICK(), TYPE("你好"), TYPE("世界"), COMPLETE]
        with runner_env(actions,
                        overrides={"read_focused_text": lambda device=None: None}) as h:
            rc = run_task("打开设置", max_steps=4)
        self.assertEqual(len(h.exec_calls), 3, "读不到输入框时应保守拦下")


class 黑屏时停下问人工(unittest.TestCase):
    """★ Claude 审查 阅读6：连续全黑 -> 退出码 4，不盲操作。"""

    def test_连续黑屏退出码4(self):
        from fakes import FakeImage
        black = FakeImage(color=(0, 0, 0))
        with runner_env([CLICK()], overrides={"screenshot": lambda device=None: black}) as h:
            rc = run_task("打开设置", max_steps=5)
        self.assertEqual(rc, 4, "连续黑屏应停下问人工")
        self.assertTrue(h.report_calls)
        # BLANK_FRAME_MAX=2：第 1 步先观察（防把深色界面误判成黑屏），第 2 步才停
        self.assertLessEqual(h.model_calls, 1, "连续黑屏应尽快停下，不该一直烧模型")


class 退出路径都有结论行(unittest.TestCase):
    """★ mimo P1-6：5 条退出路径都必须留 [结论] 行，否则调用方会自己编原因。"""

    def test_步数耗尽(self):
        with runner_env([CLICK()]) as h:
            rc = run_task("打开设置", max_steps=2)
        self.assertEqual(rc, 1)
        self.assertIn("[结论]", h.output)

    def test_卡死(self):
        with runner_env([CLICK(500, 800)] * 6) as h:
            rc = run_task("打开设置", max_steps=10)
        self.assertEqual(rc, 2)
        self.assertIn("[结论]", h.output)

    def test_TYPE失败(self):
        with runner_env([TYPE("你好")],
                        exec_fn=lambda a: ExecResult(ok=False, note="输入失败")) as h:
            rc = run_task("给张三发消息", max_steps=5)
        self.assertEqual(rc, 2)
        self.assertIn("[结论]", h.output)

    def test_模型调用失败(self):
        def boom(*a, **k):
            raise RuntimeError("model down")
        with runner_env([CLICK()], overrides={"ask_model": boom}) as h:
            rc = run_task("打开设置", max_steps=3)
        self.assertEqual(rc, 1)
        self.assertIn("[结论]", h.output)

    def test_执行抛AdbError(self):
        from phone_agent.adb import AdbError

        def boom(action, *a, **k):
            raise AdbError("device offline")

        with runner_env([CLICK()], overrides={"execute_action": boom}) as h:
            rc = run_task("打开设置", max_steps=3)
        self.assertEqual(rc, 1)
        self.assertIn("[结论]", h.output)


class dry_run不碰手机(unittest.TestCase):
    """★ mimo P1-2：dry-run 不该往手机 push 任何东西。"""

    def test_dry_run不调用ensure_yadb(self):
        calls: list[int] = []

        def fake_yadb(*a, **k):
            calls.append(1)
            return True

        with runner_env([CLICK()], overrides={"ensure_yadb": fake_yadb}) as h:
            rc = R.run(task="打开设置", device=None, client=object(), model="m",
                       view_width=720, max_steps=2, step_delay=0.0,
                       auto_launch=False, dry_run=True)
        self.assertEqual(calls, [], "dry-run 不该调用 ensure_yadb（会往手机 push）")
        self.assertIn("dry-run", h.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
