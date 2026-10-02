"""第三方依赖的统一导入点 —— 缺失时给出明确提示，避免每个模块各写一遍。"""

import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("缺少依赖 Pillow，请先执行：pip install pillow openai")

try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺少依赖 openai，请先执行：pip install pillow openai")

__all__ = ["Image", "OpenAI"]
