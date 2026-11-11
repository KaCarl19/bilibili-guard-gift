"""QQ 机器人
群里：新人进群提示、截图验证舰长、「绑定 UID」
私聊（好友 + 陌生人临时会话）：识别改地址/手机号请求 → 多轮提问确认本月上舰 → 发填写链接
"""
import asyncio
import re

import httpx
from nonebot import get_bots, get_driver, on_command, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot, Event, FriendRequestEvent, GroupIncreaseNoticeEvent, GroupMessageEvent, Message,
    MessageEvent, MessageSegment, PrivateMessageEvent,
)
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

from app import bili, db, dialog, notify, verify
from app.config import QQ, QQ_ADMINS, QQ_GROUPS, WEB

driver = get_driver()
NEXT_STEP = "\n\n📦 填写收货地址和手机号：私聊我发送「填地址」（不加好友也可以通过群临时会话私聊）。"


# ---------------- 规则 ----------------
def _in_group(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and event.group_id in QQ_GROUPS


def _is_private(event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent)


def _is_admin(event: Event) -> bool:
    return isinstance(event, MessageEvent) and event.user_id in QQ_ADMINS


def _is_join(event: Event) -> bool:
    return isinstance(event, GroupIncreaseNoticeEvent) and event.group_id in QQ_GROUPS


def _is_friend_req(event: Event) -> bool:
    return isinstance(event, FriendRequestEvent)


def _group_image(event: Event) -> bool:
    return _in_group(event) and any(s.type == "image" for s in event.message)


def _verified_this_month(qq) -> bool:
    return any(r["month"] == db.this_month() and r["status"] == "verified" for r in db.guards_of_qq(qq))


async def _download(event: MessageEvent) -> list[bytes]:
    out = []
    for seg in event.message:
        if seg.type != "image":
            continue
        url = seg.data.get("url") or seg.data.get("file")
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                out.append((await c.get(url)).content)
        except Exception as e:
            logger.warning(f"下载图片失败：{e}")
    return out


async def _at_reply(bot: Bot, event: GroupMessageEvent, text: str):
    await bot.send(event, MessageSegment.at(event.user_id) + " " + text)


# ================= 群聊 =================
join_notice = on_notice(rule=Rule(_is_join))


@join_notice.handle()
async def _(bot: Bot, event: GroupIncreaseNoticeEvent):
    if event.user_id == event.self_id or _verified_this_month(event.user_id):
        return
    db.add_join(event.user_id, event.group_id)
    mins = int(QQ.get("kick_unverified_minutes", 0) or 0)
    tip = f"\n⏰ {mins} 分钟内未验证将被移出群聊。" if mins else ""
    await bot.send(event, MessageSegment.at(event.user_id) + (
        " 欢迎来到舰长群！请在群里发送你的上舰截图（需包含主播名、舰长字样、开通时间）完成验证。" + tip))


group_shot = on_message(rule=Rule(_group_image), priority=20, block=False)


@group_shot.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    qq = event.user_id
    if _verified_this_month(qq):
        return                                   # 已验证的人发图不处理，避免刷屏
    newcomer = db.is_joining(qq)
    for img in await _download(event):
        result, text, _g = await verify.verify_screenshot(qq, img)
        logger.info(f"QQ {qq} 群截图验证结果：{result}")
        if result == "not_guard" and not newcomer:
            continue                             # 老成员随手发的图，识别不出就别打扰
        if result == "verified":
            db.remove_join(qq)
            text += NEXT_STEP
        elif result == "pending":
            text += "\n请发送「绑定 你的B站UID」（例如：绑定 12345678）自动核验，或等待主播人工审核。"
            await notify.notify_admins(f"📝 待审核：QQ {qq} 在群 {event.group_id} 发了上舰截图，但没在舰长列表里匹配到。")
        await _at_reply(bot, event, text)
        if result in ("verified", "pending"):
            break


bind = on_command("绑定", rule=Rule(_in_group), priority=5, block=True)


@bind.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    uid = re.sub(r"\D", "", arg.extract_plain_text())
    if not uid:
        await _at_reply(bot, event, "用法：绑定 你的B站UID（在B站「我的」页面可以看到UID）")
        return
    ok, text = await verify.bind_uid(event.user_id, uid)
    if ok:
        db.remove_join(event.user_id)
        text += NEXT_STEP
    await _at_reply(bot, event, text)


group_fill = on_command("填地址", aliases={"修改地址", "改地址", "填写地址"}, rule=Rule(_in_group),
                        priority=5, block=True)


@group_fill.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    await _at_reply(bot, event, "地址属于隐私，请私聊我发送「改地址」哦～（不加好友也能通过群临时会话私聊）")


# ================= 私聊（好友 + 陌生人临时会话） =================
me = on_command("我的信息", rule=Rule(_is_private), priority=5, block=True)


@me.handle()
async def _(event: PrivateMessageEvent):
    rows = db.guards_of_qq(event.user_id)[:3]
    if not rows:
        await me.finish("还没有你的上舰记录。需要填写/修改收货地址请发「改地址」。")
    st = {"verified": "已验证", "pending": "待审核", "rejected": "已拒绝"}
    lines = [f"{r['month']}：{r['guard_level'] or ''} 上舰日期 {r['start_date']}（{st[r['status']]}）" for r in rows]
    a = db.get_address(event.user_id)
    if a:
        lines.append(f"收件人：{a['receiver']}  手机：{a['phone'][:3]}****{a['phone'][-4:]}")
        lines.append(f"地址：{a['address']}")
    else:
        lines.append("还未填写收货地址")
    await me.finish("\n".join(lines))


stat = on_command("舰长统计", rule=Rule(_is_admin), priority=5, block=True)


@stat.handle()
async def _():
    rows = db.list_guards(db.this_month())
    v = [r for r in rows if r["status"] == "verified"]
    p = [r for r in rows if r["status"] == "pending"]
    no_addr = [r for r in v if not r["address"]]
    msg = (f"{db.this_month()} 舰长：已验证 {len(v)}，待审核 {len(p)}，已验证但未填地址 {len(no_addr)}\n"
           f"后台：{WEB['public_url'].rstrip('/')}/admin")
    if no_addr:
        msg += "\n未填地址：" + "、".join(r["bili_name"] or r["qq"] for r in no_addr[:30])
    await stat.finish(msg)


private_chat = on_message(rule=Rule(_is_private), priority=10, block=True)


@private_chat.handle()
async def _(bot: Bot, event: PrivateMessageEvent):
    qq = str(event.user_id)
    if event.sub_type == "group":                      # 陌生人通过群发起的临时会话
        gid = getattr(event.sender, "group_id", None) or (event.model_extra or {}).get("group_id") \
            or (event.model_extra or {}).get("temp_source")
        if gid:
            notify.TEMP_GROUP[qq] = int(gid)
    text = event.get_plaintext()
    images = await _download(event) if any(s.type == "image" for s in event.message) else []
    out = await dialog.handle(qq, text, images)
    for m in out.msgs:
        await notify.send_private(qq, m)
    for m in out.admin:
        await notify.notify_admins(m)


# ---------------- 自动同意好友申请 ----------------
friend_req = on_request(rule=Rule(_is_friend_req))


@friend_req.handle()
async def _(bot: Bot, event: FriendRequestEvent):
    await event.approve(bot)


# ---------------- 超时未验证踢人 ----------------
async def _kick_loop():
    mins = int(QQ.get("kick_unverified_minutes", 0) or 0)
    while mins:
        await asyncio.sleep(60)
        try:
            for j in db.expired_joins(mins):
                if any(r["month"] == db.this_month() and r["status"] == "pending"
                       for r in db.guards_of_qq(j["qq"])):
                    continue                     # 截图已提交、等主播审核的人不踢
                for bot in get_bots().values():
                    try:
                        await bot.set_group_kick(group_id=int(j["group_id"]), user_id=int(j["qq"]))
                        logger.info(f"踢出未验证成员 {j['qq']}")
                    except Exception as e:
                        logger.warning(f"踢人失败 {j['qq']}：{e}")
                db.remove_join(j["qq"])
        except Exception as e:
            logger.exception(e)


@driver.on_startup
async def _startup():
    db.init()
    asyncio.create_task(_kick_loop())
    asyncio.create_task(bili.guard_list())   # 预热：顺便检查 B站 接口是否可用
