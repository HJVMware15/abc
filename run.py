import os
from dotenv import load_dotenv

# 加载.env文件中的环境变量（如果存在）
load_dotenv()

# 导入主程序
from main import bot, TOKEN

# 运行机器人
bot.run(os.environ.get("DISCORD_BOT_TOKEN", TOKEN))
