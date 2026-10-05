"""
回归测试 —— 把 2026-10-05 修过的一批「真机坑」冻住，防止以后改回去。

覆盖：
  * 坐标越界拒绝（③）      normalize_action 的 CLICK/SLIDE
  * WAIT 秒数范围（④）      normalize_action 的 WAIT
  * App 精确解析（⑤/#1）    resolve_package 只认精确别名/包名
  * 纯打开判定的别名顺序（#3 顺带修的既有 bug）
  * 日志文件同秒撞名加后缀（⑧）

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 `import fakes` 可用

from fakes import alias_env                                   # noqa: E402

from phone_agent import output                                # noqa: E402
from phone_agent.apps import resolve_package                  # noqa: E402
from phone_agent.tasks import _is_pure_open_task              # noqa: E402
from phone_agent.vision import InvalidActionError, normalize_action   # noqa: E402


class 坐标范围(unittest.TestCase):
    def test_容差内通过(self):
        for p in ([500, 800], [1002, 500], [-50, 500]):
            out = normalize_action({"action_type": "CLICK", "point": p})
            self.assertEqual(out["point"], (float(p[0]), float(p[1])))

    def test_明显越界拒绝(self):
        for p in ([-100, 500], [99999, 99999]):
            with self.assertRaises(InvalidActionError):
                normalize_action({"action_type": "CLICK", "point": p})

    def test_SLIDE起终点也校验(self):
        with self.assertRaises(InvalidActionError):
            normalize_action({"action_type": "SLIDE",
                              "point1": [500, 5000], "point2": [500, 200]})


class WAIT范围(unittest.TestCase):
    def test_缺省3秒(self):
        self.assertEqual(normalize_action({"action_type": "WAIT"})["seconds"], 3.0)

    def test_边界0和10通过(self):
        for s in (0, 10):
            self.assertEqual(
                normalize_action({"action_type": "WAIT", "seconds": s})["seconds"], float(s))

    def test_越界拒绝(self):
        # 负数会让 time.sleep 抛错；过大无意义 —— 都判非法、交给重试
        for s in (-100, 999999):
            with self.assertRaises(InvalidActionError):
                normalize_action({"action_type": "WAIT", "seconds": s})


class App精确解析(unittest.TestCase):
    def test_精确别名命中(self):
        with alias_env():
            self.assertEqual(resolve_package("微信"), "com.tencent.mm")
            self.assertEqual(resolve_package("WeChat"), "com.tencent.mm")   # 大小写不敏感

    def test_模糊名一律拒绝(self):
        # 旧版「双向包含」会把「微信读书」误判成微信 → 启动错误的 App
        with alias_env():
            for name in ("微信读书", "qq音乐", "网易云音乐", "收藏"):
                self.assertIsNone(resolve_package(name), f"{name} 不该被解析出来")


class 纯打开别名顺序(unittest.TestCase):
    def test_长别名先剥(self):
        # 旧实现按字典序剥别名，「设置」会先把「系统设置」剥成「系统」→ 误判非纯打开
        with alias_env():
            self.assertTrue(_is_pure_open_task("打开系统设置", "com.android.settings"))
            self.assertTrue(_is_pure_open_task("打开设置", "com.android.settings"))

    def test_带别的要求就不是纯打开(self):
        with alias_env():
            self.assertFalse(_is_pure_open_task("打开设置里把蓝牙关了", "com.android.settings"))


class 日志文件名去重(unittest.TestCase):
    def test_同秒撞名加后缀(self):
        tmp = Path(tempfile.mkdtemp(prefix="pa_log_"))
        try:
            with mock.patch.object(output, "LOG_DIR", tmp):
                handles = [output._open_log() for _ in range(3)]
                names = [os.path.basename(h.name) for h in handles]
            for h in handles:
                h.close()
        finally:
            for p in tmp.glob("*"):
                p.unlink()
            tmp.rmdir()
        self.assertEqual(len(set(names)), 3, f"同名了：{names}")
        self.assertTrue(any(n.endswith("_1.log") for n in names), names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
