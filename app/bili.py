"""调用 B站 公开接口获取直播间当前在舰列表（无需登录）"""
import asyncio
import time

import httpx
from nonebot.log import logger

from .config import STREAMER

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}
LEVEL_NAME = {1: "总督", 2: "提督", 3: "舰长"}

_room = {}
_cache = {"t": 0, "data": None}


async def _room_info(client):
    if not _room:
        r = (await client.get("https://api.live.bilibili.com/room/v1/Room/get_info",
                              params={"room_id": STREAMER["room_id"]})).json()
        _room["room_id"] = r["data"]["room_id"]
        _room["uid"] = r["data"]["uid"]
    return _room["room_id"], _room["uid"]


async def guard_list(force=False):
    """返回 {uid(str): {"uid","name","level"}}；接口失败返回 None"""
    # 平时缓存 5 分钟；强制刷新也至少间隔 60 秒，避免被 B站 限流
    if _cache["data"] is not None and time.time() - _cache["t"] < (60 if force else 300):
        return _cache["data"]
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
            room_id, ruid = await _room_info(client)
            result, page, pages = {}, 1, 1
            while page <= pages and page <= 50:
                j = await _get_page(client, room_id, ruid, page)
                d = j.get("data") or {}
                pages = int((d.get("info") or {}).get("page") or 1)
                items = (d.get("top3") or []) + (d.get("list") or []) if page == 1 else (d.get("list") or [])
                for it in items:
                    uinfo = it.get("uinfo") or {}
                    uid = str(it.get("uid") or uinfo.get("uid") or "")
                    name = it.get("username") or (uinfo.get("base") or {}).get("name") or ""
                    level = it.get("guard_level") or (uinfo.get("guard") or {}).get("level") or 3
                    if uid:
                        result[uid] = {"uid": uid, "name": name, "level": LEVEL_NAME.get(int(level), "舰长")}
                page += 1
        _cache.update(t=time.time(), data=result)
        logger.info(f"B站在舰列表已刷新，共 {len(result)} 人")
        return result
    except Exception as e:
        logger.warning(f"获取B站在舰列表失败：{e}")
        return _cache["data"]          # 失败时沿用上一次成功的列表（没有则为 None）


async def _get_page(client, room_id, ruid, page):
    """单页请求，被限流时等一下重试"""
    for attempt in range(3):
        if page > 1 or attempt:
            await asyncio.sleep(0.4 + attempt * 1.5)
        try:
            j = (await client.get(
                "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList",
                params={"roomid": room_id, "ruid": ruid, "page": page, "page_size": 30})).json()
            if j.get("code") == 0:
                return j
            err = j.get("message")
        except Exception as e:
            err = e
    raise RuntimeError(f"第{page}页请求失败：{err}")
