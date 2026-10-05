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


if __name__ == "__main__":
    unittest.main(verbosity=2)
