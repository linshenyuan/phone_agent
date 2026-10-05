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
from phone_agent.adb import pick_device                       # noqa: E402
from phone_agent.apps import resolve_package                  # noqa: E402
from phone_agent.config import DURATION_MAX, DURATION_MIN     # noqa: E402
from phone_agent.tasks import (_looks_like_text_task,         # noqa: E402
                               _is_pure_open_task)
from phone_agent.vision import (InvalidActionError,           # noqa: E402
                                is_loopback_endpoint, model_name_matches,
                                normalize_action, parse_action)


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


class 动作名白名单(unittest.TestCase):
    def test_不认识的动作当场拒绝(self):
        # 旧实现会「归一化成功」，到 execute_action 才失败 —— 白烧一步且不走重试
        for bad in ("ENTER", "SWIPE_UP", "CLICKX"):
            with self.assertRaises(InvalidActionError):
                normalize_action({"action_type": bad})

    def test_别名仍可用(self):
        self.assertEqual(
            normalize_action({"action_type": "SCROLL",
                              "point1": (500, 800), "point2": (500, 200)})["action_type"],
            "SLIDE")
        self.assertEqual(
            normalize_action({"action_type": "LAUNCH", "app": "微信"})["action_type"],
            "OPEN")


class duration钳制(unittest.TestCase):
    def _slide(self, **kw):
        a = {"action_type": "SLIDE", "point1": (500, 800), "point2": (500, 200)}
        a.update(kw)
        return normalize_action(a)["duration"]

    def test_过大钳到上限(self):
        # duration:1000 旧版原样传下去 → input swipe 撞 30s 超时、整轮中止
        self.assertEqual(self._slide(duration=1000), DURATION_MAX)

    def test_过小钳到下限(self):
        self.assertEqual(self._slide(duration=0.01), DURATION_MIN)

    def test_合法值原样保留(self):
        self.assertEqual(self._slide(duration=2), 2.0)

    def test_不传用默认(self):
        self.assertGreater(self._slide(), 0)


class COMPLETE保留结果(unittest.TestCase):
    def test_value被保留(self):
        # 旧版没有 COMPLETE 分支 → result_text 永远是空串（死代码）
        out = normalize_action({"action_type": "COMPLETE", "value": "明天晴"})
        self.assertEqual(out.get("value"), "明天晴")

    def test_return也算(self):
        out = normalize_action({"action_type": "COMPLETE", "return": "晴"})
        self.assertEqual(out.get("value"), "晴")


class KEY_VALUE不截断(unittest.TestCase):
    def test_值里的伪键不再被切断(self):
        # ★ Claude 审查 #1：旧实现会把值从 ", address:" 处切断，而截断后
        #   仍能通过回读校验 → 半截消息被发出去，还算成功
        a = parse_action("action:TYPE\tvalue:meet me at the cafe, address: 12 Main St")
        self.assertEqual(a.get("value"), "meet me at the cafe, address: 12 Main St")

    def test_note同样不被切(self):
        a = parse_action("action:TYPE\tvalue:see you at 5 pm note: bring ID")
        self.assertEqual(a.get("value"), "see you at 5 pm note: bring ID")

    def test_常规格式仍能解析(self):
        for raw in ("action:CLICK\tpoint:500,800", "action:CLICK point:500,800"):
            out = parse_action(raw)
            self.assertEqual(out["action_type"], "CLICK")
            self.assertEqual(out["point"], (500.0, 800.0))


class 文字任务判定(unittest.TestCase):
    def test_功能名不算要打字(self):
        # ★ Claude 审查 #3：这三个是纯打开任务，旧版判成文字任务 → 退出码 3
        for t in ("打开短信", "查看邮件", "打开输入法设置"):
            self.assertFalse(_looks_like_text_task(t), f"{t} 不该被判成文字任务")

    def test_真的要打字仍能识别(self):
        for t in ("给张三发短信", "回复邮件", "给张三发消息", "在搜索框输入蓝牙"):
            self.assertTrue(_looks_like_text_task(t), f"{t} 应被判成文字任务")


class 大小写不敏感(unittest.TestCase):
    def test_大小写混写的App名也算纯打开(self):
        # ★ Claude 审查 #9：旧版剥不掉 "WeChat" → 误判非纯打开 → 错过零模型短路
        with alias_env():
            self.assertTrue(_is_pure_open_task("打开WeChat", "com.tencent.mm"))
            self.assertTrue(_is_pure_open_task("打开wechat", "com.tencent.mm"))

    def test_带别的要求仍不算纯打开(self):
        with alias_env():
            self.assertFalse(_is_pure_open_task("打开设置里把蓝牙关了", "com.android.settings"))


class 模型名匹配(unittest.TestCase):
    def test_宽松只给本地(self):
        # 云端必须精确：旧版 "gpt-4o" vs ["gpt-4"] 会误判成 True
        self.assertFalse(model_name_matches("gpt-4o", ["gpt-4"], loose=False))
        self.assertFalse(model_name_matches("gpt-4o", ["gpt-4o-mini"], loose=False))
        self.assertTrue(model_name_matches("gpt-4o", ["gpt-4o"], loose=False))

    def test_本地保留别名兜底(self):
        # llama-server 不带 -Alias 时别名由文件名推导，长名要能匹配短名
        self.assertTrue(model_name_matches(
            "GELab-Zero", ["stepfun-ai_GELab-Zero-4B-preview"], loose=True))

    def test_回环端点判定(self):
        self.assertTrue(is_loopback_endpoint("http://127.0.0.1:8080/v1"))
        self.assertTrue(is_loopback_endpoint("http://localhost:8080/v1"))
        self.assertFalse(is_loopback_endpoint("https://api.openai.com/v1"))


class 多设备不静默挑第一台(unittest.TestCase):
    def test_多设备未指定则报错(self):
        # ★ Claude 审查 #4：会发消息的工具，操作错手机是最糟的结果
        import phone_agent.adb as adb_mod
        with mock.patch.object(adb_mod, "list_devices", return_value=["dev1", "dev2"]):
            with self.assertRaises(SystemExit):
                pick_device(None)

    def test_单设备照常返回(self):
        import phone_agent.adb as adb_mod
        with mock.patch.object(adb_mod, "list_devices", return_value=["only"]):
            self.assertEqual(pick_device(None), "only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
