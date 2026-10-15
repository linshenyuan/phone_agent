"""
UI 树直控支持（P1）单测 —— 只测纯函数，不碰真机、不调 adb、不调模型。

覆盖：
  * _parse_ui_nodes 补的属性（resource-id / content-desc / clickable / enabled）
  * is_tree_usable 回退闸门
  * find_nodes 定位
  * node_center
  * render_tree_text
  * dump_nodes（mock _dump_ui）

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 `import fakes` 可用

from phone_agent import ui                                    # noqa: E402


# 一份贴近 uiautomator dump 真实输出的样本（4 个节点：可点按钮 / 无文字图标 /
# 不可点文本 / 密码框）。
SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="发送" resource-id="com.app:id/send" class="android.widget.Button" package="com.app" content-desc="" clickable="true" enabled="true" focusable="true" focused="false" password="false" bounds="[900,2100][1050,2200]" />
  <node index="1" text="" resource-id="" class="android.widget.ImageButton" package="com.app" content-desc="返回" clickable="true" enabled="true" focusable="true" focused="false" password="false" bounds="[0,0][120,120]" />
  <node index="2" text="聊天记录" resource-id="" class="android.widget.TextView" package="com.app" content-desc="" clickable="false" enabled="true" focusable="false" focused="false" password="false" bounds="[100,300][600,400]" />
  <node index="3" text="请输入密码" resource-id="com.app:id/pwd" class="android.widget.EditText" package="com.app" content-desc="" clickable="true" enabled="true" focusable="true" focused="true" password="true" bounds="[100,500][900,600]" />
</hierarchy>
"""


class 节点解析(unittest.TestCase):
    def test_补的新属性(self):
        nodes = ui._parse_ui_nodes(SAMPLE_XML)
        self.assertEqual(len(nodes), 4)
        n0 = nodes[0]
        self.assertEqual(n0["text"], "发送")
        self.assertEqual(n0["resource_id"], "com.app:id/send")
        self.assertTrue(n0["clickable"])
        self.assertTrue(n0["enabled"])
        self.assertEqual(n0["bounds"], (900, 2100, 1050, 2200))

    def test_content_desc与不可点(self):
        nodes = ui._parse_ui_nodes(SAMPLE_XML)
        self.assertEqual(nodes[1]["content_desc"], "返回")
        self.assertEqual(nodes[1]["text"], "")
        self.assertFalse(nodes[2]["clickable"])

    def test_密码属性仍在(self):
        nodes = ui._parse_ui_nodes(SAMPLE_XML)
        self.assertTrue(nodes[3]["password"])


class 回退闸门(unittest.TestCase):
    def test_None与空不可用(self):
        self.assertFalse(ui.is_tree_usable(None))
        self.assertFalse(ui.is_tree_usable([]))

    def test_全容器不可用(self):
        # 自绘界面（vivo 桌面）：节点多但全是无 text/desc 的容器
        nodes = [{"clickable": True, "bounds": (0, 0, 10, 10),
                  "text": "", "content_desc": ""} for _ in range(45)]
        self.assertFalse(ui.is_tree_usable(nodes))

    def test_带标签可用(self):
        self.assertTrue(ui.is_tree_usable(ui._parse_ui_nodes(SAMPLE_XML)))

    def test_只有一个带标签不可用(self):
        nodes = [{"clickable": True, "bounds": (0, 0, 10, 10),
                  "text": "唯一", "content_desc": ""}]
        self.assertFalse(ui.is_tree_usable(nodes))


class 元素定位(unittest.TestCase):
    def setUp(self):
        self.nodes = ui._parse_ui_nodes(SAMPLE_XML)

    def test_按text(self):
        hit = ui.find_nodes(self.nodes, text="发送")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["resource_id"], "com.app:id/send")

    def test_按desc(self):
        hit = ui.find_nodes(self.nodes, desc="返回")
        self.assertEqual(len(hit), 1)

    def test_按res_id(self):
        hit = ui.find_nodes(self.nodes, res_id="send")
        self.assertEqual(len(hit), 1)

    def test_不可点默认被排除(self):
        # "聊天记录" 是 TextView（clickable=false）-> 默认排除
        self.assertEqual(ui.find_nodes(self.nodes, text="聊天记录"), [])
        self.assertEqual(
            len(ui.find_nodes(self.nodes, text="聊天记录", clickable_only=False)), 1)


class 坐标与文本(unittest.TestCase):
    def setUp(self):
        self.nodes = ui._parse_ui_nodes(SAMPLE_XML)

    def test_节点中心(self):
        self.assertEqual(ui.node_center(self.nodes[0]), (975, 2150))

    def test_无bounds返回None(self):
        self.assertIsNone(ui.node_center({"bounds": None}))

    def test_树文本标注可点只读(self):
        txt = ui.render_tree_text(self.nodes)
        self.assertIn("[可点] 发送", txt)
        self.assertIn("[可点] 返回", txt)
        self.assertIn("[只读] 聊天记录", txt)


class dump入口(unittest.TestCase):
    def test_拿不到树返回None(self):
        with mock.patch.object(ui, "_dump_ui", return_value=None):
            self.assertIsNone(ui.dump_nodes())

    def test_拿到树返回节点(self):
        with mock.patch.object(ui, "_dump_ui", return_value=SAMPLE_XML):
            self.assertEqual(len(ui.dump_nodes()), 4)


class B通道(unittest.TestCase):
    """P2：树可用 -> 提示词带树文本；树不可用 -> 只截图（保留截图）。"""

    def _run(self, actions, **overrides):
        from fakes import runner_env
        from phone_agent import runner as R
        with runner_env(actions, overrides=overrides or None) as h:
            R.run(task="打开设置", device=None, client=object(), model="m",
                  view_width=720, max_steps=3, step_delay=0.0, auto_launch=False)
        return h

    def test_树可用时带树文本(self):
        nodes = [{"clickable": True, "bounds": (0, 0, 10, 10),
                  "text": "发送", "content_desc": ""}]
        h = self._run(
            [{"action_type": "COMPLETE"}],
            dump_nodes=lambda device=None: nodes,
            is_tree_usable=lambda n: True,
            render_tree_text=lambda n, limit=40, max_len=30: "[可点] 发送",
        )
        self.assertTrue(h.tree_texts, "应记录每次问模型的 tree_text")
        self.assertIn("发送", h.tree_texts[0], "树可用时应带树文本问模型")

    def test_树不可用时不带树文本(self):
        h = self._run([{"action_type": "COMPLETE"}])
        self.assertTrue(all(t == "" for t in h.tree_texts),
                        "树不可用时应只走截图，不带树文本")

    def test_dry_run不dump树(self):
        # dry-run 不碰手机 -> 不该调用 dump_nodes
        calls: list[int] = []

        def spy(device=None):
            calls.append(1)
            return None

        from fakes import runner_env
        from phone_agent import runner as R
        with runner_env([{"action_type": "COMPLETE"}],
                        overrides={"dump_nodes": spy}) as h:
            R.run(task="打开设置", device=None, client=object(), model="m",
                  view_width=720, max_steps=3, step_delay=0.0,
                  auto_launch=False, dry_run=True)
        self.assertEqual(calls, [], "dry-run 不该调用 dump_nodes（会碰手机）")


class 规则层(unittest.TestCase):
    """P3：单步点击任务在树里唯一定位 -> 免模型；其余交回模型。"""

    def setUp(self):
        self.nodes = ui._parse_ui_nodes(SAMPLE_XML)
        self.size = (1080, 2408)

    def test_唯一命中生成CLICK(self):
        act = ui.try_rule_click("点击发送", self.nodes, self.size)
        self.assertIsNotNone(act)
        self.assertEqual(act["action_type"], "CLICK")
        # 发送 bounds (900,2100,1050,2200) 中心 (975,2150)
        # 975/1080*1000 = 903, 2150/2408*1000 = 893
        self.assertEqual(act["point"], (903.0, 893.0))

    def test_按content_desc定位(self):
        # 「返回」是无文字图标按钮，text 为空，只能靠 content-desc
        act = ui.try_rule_click("点击返回", self.nodes, self.size)
        self.assertIsNotNone(act)

    def test_非单步返回None(self):
        self.assertIsNone(
            ui.try_rule_click("打开微信给张三发消息", self.nodes, self.size))

    def test_零命中返回None(self):
        self.assertIsNone(
            ui.try_rule_click("点击不存在的按钮", self.nodes, self.size))

    def test_空树返回None(self):
        self.assertIsNone(ui.try_rule_click("点击发送", None, self.size))

    def test_runner_规则命中免模型_且不重复点(self):
        from fakes import runner_env
        from phone_agent import runner as R
        with runner_env([{"action_type": "COMPLETE"}],
                        overrides={
                            "dump_nodes": lambda device=None: self.nodes,
                            "is_tree_usable": lambda n: True,
                            "render_tree_text": lambda n, limit=40, max_len=30: "",
                        }) as h:
            R.run(task="点击发送", device=None, client=object(), model="m",
                  view_width=720, max_steps=3, step_delay=0.0, auto_launch=False)
        # 第 1 步：规则命中 -> CLICK（免模型）；第 2 步：同目标已点 -> 交模型 -> COMPLETE
        self.assertEqual(h.exec_calls[0]["action_type"], "CLICK",
                         "第 1 步应是规则生成的 CLICK")
        self.assertEqual(h.model_calls, 1,
                         "规则命中的那一步不该调模型（只后续调 1 次）")
        self.assertEqual(len(h.exec_calls), 2, "同一目标只该规则点一次")


if __name__ == "__main__":
    unittest.main()
