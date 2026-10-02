"""让 `python -m phone_agent` 能直接跑。"""

import sys

from .phone_agent import main

if __name__ == "__main__":
    sys.exit(main())
