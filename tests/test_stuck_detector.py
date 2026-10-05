"""
`StuckDetector` 的独立测试（2026-10-05 加）。

它是全项目**真机经验最密集**的模块（连续 TYPE 保护 / 滑动上限 / 点击打转 /
升级人工），却一直没有独立测试。这里把它的**当前实测行为**锁住 ——
以后要改它，先看这些用例，别把真机踩出来的行为改坏。

⚠️ 用例写的是**实测行为**，不是「理想行为」。已知与旧注释不符的一处：
   `A B A B` 两个点来回跳**不会**被判卡死（见 `test_两个点来回跳_当前不判卡死`）。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fakes import FakeImage                                         # noqa: E402

from phone_agent.actions import (StuckDetector, is_blank_frame,     # noqa: E402
                                 screen_fingerprint)
from phone_agent.config import (MAX_CONSECUTIVE_TYPE,               # noqa: E402
                                MAX_SLIDE_TOTAL, STUCK_REPEAT_THRESHOLD)


def C(x: float, y: float) -> dict:
    return {"action_type": "CLICK", "point": (x, y)}


def T(text: str = "你好") -> dict:
    return {"action_type": "TYPE", "value": text}


def S(y1: float = 800, y2: float = 200) -> dict:
    return {"action_type": "SLIDE", "point1": (500, y1), "point2": (500, y2)}


def sd() -> StuckDetector:
    return StuckDetector(1080, 2408)


class 点击打转(unittest.TestCase):
    def test_同一点连点三次_判卡死(self):
        d = sd()
        reason = None
        for _ in range(STUCK_REPEAT_THRESHOLD):
            reason = d.update(C(500, 800)) or reason
        self.assertIsNotNone(reason, "同一点连点应判卡死")

    def test_容差内算同一点(self):
        d = sd()
        reason = None
        for x in (500, 501, 500):          # 1080 宽下 ±1 ≈ 1px，远在 30px 容差内
            reason = d.update(C(x, 800)) or reason
        self.assertIsNotNone(reason, "容差内的点应算同一处")

    def test_两个点来回跳_当前不判卡死(self):
        # ★ 锁「实测行为」：众数只有 2，够不到阈值 3（见文件头说明）
        d = sd()
        for x in (200, 800, 200, 800):
            self.assertIsNone(d.update(C(x, 400)), "A B A B 当前不应判卡死")
        self.assertEqual(d._count_stuck_clicks(), 2)

    def test_中间夹无关动作_不清空窗口(self):
        # 旧实现的缺陷之一：别的动作会把窗口清空，导致打转识别不出来
        d = sd()
        d.update(C(500, 800))
        d.update(C(500, 800))
        d.update({"action_type": "WAIT", "seconds": 1})
        self.assertIsNotNone(d.update(C(500, 800)), "WAIT 不该清空点击窗口")


class 连续TYPE保护(unittest.TestCase):
    def test_连打第二次_被拦但不升级(self):
        d = sd()
        d.update(C(500, 800))
        d.update(T())
        d.update(T())                       # consecutive_type -> 2
        self.assertTrue(d.should_block(T()))
        self.assertFalse(d.escalate_stop, "普通拦截不该升级为停下报告")

    def test_重新点输入框后可以再打(self):
        d = sd()
        d.update(C(500, 800), click_on_input=True, click_checked=True)
        d.update(T())
        d.update(C(500, 800), click_on_input=True, click_checked=True)   # 重新聚焦 -> 清零
        d.update(T())
        self.assertFalse(d.should_block(T()), "点过输入框后应允许再输入")
        self.assertEqual(d.consecutive_type, MAX_CONSECUTIVE_TYPE)

    def test_看清了不是输入框_不清零(self):
        d = sd()
        d.update(C(500, 800), click_on_input=False, click_checked=True)   # 明确没命中
        d.update(T())
        d.update(T())
        self.assertTrue(d.should_block(T()))
        self.assertFalse(d.escalate_stop, "「确定不是输入框」不该升级为停下报告")

    def test_看不清输入框时连打第二次_升级人工(self):
        # 2026-10-04 加：界面读不透（Flutter/WebView）时不猜，直接停下让人接手
        d = sd()
        d.update(C(500, 800), click_on_input=None, click_checked=True)    # 看不清
        d.update(T())
        d.update(T())
        self.assertTrue(d.should_block(T()))
        self.assertTrue(d.escalate_stop, "看不清 + 连打第二次应升级")


class 滑动(unittest.TestCase):
    def test_超过总次数_被拦(self):
        d = sd()
        for _ in range(MAX_SLIDE_TOTAL + 1):
            d.update(S())
        self.assertGreater(d.slide_total, MAX_SLIDE_TOTAL)
        self.assertTrue(d.should_block(S()))

    def test_同一次滑动反复_判卡死(self):
        d = sd()
        reason = None
        for _ in range(3):
            reason = d.update(S()) or reason
        self.assertIsNotNone(reason, "同一次滑动反复应判卡死（页面没滚动）")


class 界面指纹与卡死(unittest.TestCase):
    """★ 同点连点**且界面没变**才算卡死。"""

    def test_同点且界面没变_才判卡死(self):
        d = sd()
        reason = None
        for _ in range(STUCK_REPEAT_THRESHOLD):
            reason = d.update(C(500, 800), screen_fp=12345) or reason
        self.assertIsNotNone(reason)

    def test_同点但界面在变_不判卡死(self):
        # 计算器连按数字 / 步进器连点 / 翻页连点 —— 界面每步都在变 = 有进展
        d = sd()
        for fp in (1, 2, 3, 4):
            self.assertIsNone(d.update(C(500, 800), screen_fp=fp),
                              "界面有变化 = 有进展，不该判卡死")

    def test_不传指纹时退回旧判据(self):
        # 向后兼容：调用方没给界面指纹时，仍按「只看点」判
        d = sd()
        reason = None
        for _ in range(STUCK_REPEAT_THRESHOLD):
            reason = d.update(C(500, 800)) or reason
        self.assertIsNotNone(reason)

    def test_指纹函数对真实图片有效(self):
        self.assertNotEqual(screen_fingerprint(FakeImage(color=(0, 0, 0))),
                            screen_fingerprint(FakeImage(color=(255, 255, 255))))
        self.assertEqual(screen_fingerprint(FakeImage(color=(10, 20, 30))),
                         screen_fingerprint(FakeImage(color=(10, 20, 30))))


class 黑屏检测(unittest.TestCase):
    """★ 锁屏 / FLAG_SECURE / 息屏会让截图全黑。"""

    def test_纯黑算黑屏(self):
        self.assertTrue(is_blank_frame(FakeImage(color=(0, 0, 0))))

    def test_正常画面不算黑屏(self):
        self.assertFalse(is_blank_frame(FakeImage(color=(32, 32, 32))))
        self.assertFalse(is_blank_frame(FakeImage(color=(255, 255, 255))))


class 发送后放行TYPE(unittest.TestCase):
    """★ 发送成功后要再打一句，不该被连续 TYPE 保护拦下。"""

    def test_回读为空则清零放行(self):
        d = sd()
        d.update(C(500, 800))
        d.update(T())
        d.update(T())
        self.assertTrue(d.should_block(T()))
        d.reset_type_guard("回读到输入框为空")
        self.assertFalse(d.should_block(T()))
        self.assertEqual(d.consecutive_type, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
