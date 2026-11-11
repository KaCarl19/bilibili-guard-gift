"""主动发私聊消息（支持非好友的群临时会话）"""
from nonebot import get_bots
from nonebot.log import logger

from .config import QQ_ADMINS

# 陌生人通过群临时会话私聊时记下来源群，回复/通知时要带上群号
TEMP_GROUP: dict[str, int] = {}


async def send_private(qq, text: str) -> bool:
    params = {"user_id": int(qq), "message": text}
    if str(qq) in TEMP_GROUP:
        params["group_id"] = TEMP_GROUP[str(qq)]
    for bot in get_bots().values():
        try:
            await bot.call_api("send_private_msg", **params)
            return True
        except Exception as e:
            logger.warning(f"给 {qq} 发私聊失败：{e}")
    return False


async def notify_admins(text: str):
    for a in QQ_ADMINS:
        await send_private(a, text)
