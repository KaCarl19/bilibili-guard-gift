"""启动入口：python bot.py
同一个进程里同时运行 QQ 机器人（NoneBot2）和地址收集网页（FastAPI）。"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)

from app import db, web  # noqa: E402  (需在 nonebot.init 之后导入)

db.init()
nonebot.get_app().include_router(web.router)
nonebot.load_plugin("app.plugins.guard_bot")

if __name__ == "__main__":
    nonebot.run()
