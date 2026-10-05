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
from phone_agent.adb import AdbError, pick_device             # noqa: E402
from phone_agent.apps import resolve_package                  # noqa: E402
from phone_agent.config import (DURATION_MAX, DURATION_MIN,   # noqa: E402
                                TYPE_CLEAR_MAX)
from phone_agent.output import redact_text                    # noqa: E402
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


class 日志脱敏(unittest.TestCase):
    def test_短文本整体打码(self):
        self.assertEqual(redact_text("你好"), "＊＊")
        self.assertEqual(redact_text(""), "")

    def test_长文本只留开头和长度(self):
        out = redact_text("我的密码是12345")
        self.assertTrue(out.startswith("我的"))
        self.assertIn("共", out)
        self.assertNotIn("12345", out, "正文不能出现在脱敏结果里")

    def test_动作日志副本脱敏(self):
        from phone_agent.runner import _for_log
        a = {"action_type": "TYPE", "value": "秘密口令"}
        self.assertNotIn("秘密口令", str(_for_log(a)))

    def test_非TYPE动作原样返回(self):
        from phone_agent.runner import _for_log
        c = {"action_type": "CLICK", "point": (1, 2)}
        self.assertEqual(_for_log(c), c)

    def test_脱敏不改原dict(self):
        # ★ 喂给模型的历史必须保留原文（模型要知道自己输过什么）
        from phone_agent.runner import _for_log
        a = {"action_type": "TYPE", "value": "秘密口令"}
        _for_log(a)
        self.assertEqual(a["value"], "秘密口令")


class adb异常留结论行(unittest.TestCase):
    def test_打印结论并返回1(self):
        import contextlib
        import io
        from phone_agent.runner import _adb_fail
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _adb_fail("截图", AdbError("device offline"))
        self.assertEqual(rc, 1)
        self.assertIn("[结论]", buf.getvalue(), "adb 出错也必须留结论行")
        self.assertIn("device offline", buf.getvalue())


class TYPE清空上限(unittest.TestCase):
    def test_上限远高于20(self):
        # ★ Claude 审查 B1：旧实现固定只清 20 个字符 —— 长消息清不干净，
        #   残留会被下一次重试**追加**在后面（还是半截 + 拼错）
        self.assertGreater(TYPE_CLEAR_MAX, 20)


class 坐标dict缺xy(unittest.TestCase):
    """★ mimo P2：缺 x/y 旧写法会静默变 (0,0) —— 点屏幕左上角。"""

    def test_缺x或y一律拒绝(self):
        for bad in ({"x": 5}, {"y": 5}, {}):
            with self.assertRaises(InvalidActionError):
                normalize_action({"action_type": "CLICK", "point": bad})

    def test_齐全时正常(self):
        out = normalize_action({"action_type": "CLICK", "point": {"x": 5, "y": 6}})
        self.assertEqual(out["point"], (5.0, 6.0))


class 黑屏阈值(unittest.TestCase):
    """★ mimo P1-4：阈值 20 会把深色主题背景 #121212(灰度18) 当黑屏。"""

    def test_深色主题不再误判(self):
        from phone_agent.actions import is_blank_frame
        from fakes import FakeImage
        self.assertFalse(is_blank_frame(FakeImage(color=(18, 18, 18))),
                         "#121212 是深色主题背景，不该算黑屏")

    def test_纯黑仍能命中(self):
        from phone_agent.actions import is_blank_frame
        from fakes import FakeImage
        self.assertTrue(is_blank_frame(FakeImage(color=(0, 0, 0))))


class 清理只删自己的图(unittest.TestCase):
    """★ mimo P2：prune_tmp 原来 glob('*.png') 会删掉用户自己放进 tmp/ 的图。"""

    def test_用户自己的png不被删(self):
        tmp = Path(tempfile.mkdtemp(prefix="pa_prune_"))
        try:
            (tmp / "用户自己的图.png").write_bytes(b"x")
            for i in range(3):
                p = tmp / f"complete_step{i}.png"
                p.write_bytes(b"x")
                os.utime(p, (1000 + i, 1000 + i))       # 让 mtime 有序
            with mock.patch.object(output, "TMP_DIR", tmp):
                output.prune_tmp(keep=1)
            self.assertTrue((tmp / "用户自己的图.png").exists(), "用户自己的图被删了")
            self.assertTrue((tmp / "complete_step2.png").exists(), "最新一张应保留")
            self.assertFalse((tmp / "complete_step0.png").exists())
        finally:
            for p in tmp.glob("*"):
                p.unlink()
            tmp.rmdir()


class TYPE成功判据(unittest.TestCase):
    """★ mimo P1-1：旧写法「框变了就算成功」→ 截断/部分上屏也判成功。"""

    def _run_type(self, box_text):
        from phone_agent import actions as A
        with mock.patch.object(A, "focus_editable_box",
                               lambda *a, **k: (True, "ok", "旧内容", False, False)), \
                mock.patch.object(A, "read_focused_text", lambda *a, **k: box_text), \
                mock.patch.object(A, "input_text", lambda *a, **k: None), \
                mock.patch.object(A, "adb", lambda *a, **k: ""), \
                mock.patch.object(A, "time", mock.MagicMock()):
            return A.execute_action({"action_type": "TYPE", "value": "你好世界"},
                                    (1080, 2408), None, True, False)

    def test_完整上屏判成功(self):
        self.assertTrue(self._run_type("你好世界").ok)

    def test_部分上屏判失败(self):
        # 只打出前两个字：框确实变了，但内容不全 —— 不该算成功
        self.assertFalse(self._run_type("你好").ok,
                         "部分上屏不该算成功（模型会据此 COMPLETE 发半截消息）")

    def test_自动插空格仍判成功(self):
        # 手机号/金额类字段会自动插空格，归一化后仍应判成功
        self.assertTrue(self._run_type("你好 世界").ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
