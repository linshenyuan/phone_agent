#!/usr/bin/env python
"""
直接运行的启动器 —— 等价于 `python -m phone_agent`。

★ 为什么需要它：包内模块之间用**相对导入**，直接跑
  `python src/phone_agent/phone_agent.py` 会因为 `__package__` 为 None 而失败
  （ImportError: attempted relative import with no known parent package）。
  这个脚本把 src/ 加进 sys.path 再调用 main()，绕开该限制。

用法：
    python run.py --task "打开设置"
    python run.py --task "打开微信给张三发消息" --quiet

或者（等价，标准做法）：
    cd src && python -m phone_agent --task "打开设置"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from phone_agent.phone_agent import main  # noqa: E402  （必须在 sys.path 之后导入）

if __name__ == "__main__":
    sys.exit(main())
