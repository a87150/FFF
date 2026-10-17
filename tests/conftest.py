"""把项目根目录加入 sys.path，保证 pytest 能直接 import 顶层模块"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
