import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，以便找到 plugins 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotAdapter)

nonebot.load_builtin_plugins("echo")
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
