"""截图识别 + 舰长校验"""
import asyncio
import hashlib
import re
from datetime import date, datetime, timedelta

from . import bili, db
from .config import BILI, SHOT_DIR, STREAMER

LEVELS = ["总督", "提督", "舰长"]
START_KW = ["开通", "购买", "支付", "下单", "上舰", "交易", "创建", "时间"]
EXPIRE_KW = ["到期", "有效期", "过期", "截止"]
DATE_RE = re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})")
UID_RE = re.compile(r"UID\s*[:：]?\s*(\d{3,})", re.I)

_engine = None


def _ocr(path) -> list[str]:
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        _engine = RapidOCR()
    r = _engine(str(path))
    return [t for t in (r.txts or ()) if t and t.strip()]


def _dates_in(s):
    out = []
    for y, m, d in DATE_RE.findall(s):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def analyze(lines: list[str]) -> dict:
    """从 OCR 文本里提取：是否本直播间、舰长等级、上舰日期、UID"""
    full = "\n".join(lines)
    compact = full.replace(" ", "")
    has_streamer = any(k and k.replace(" ", "") in compact for k in STREAMER.get("keywords", []))
    level = next((lv for lv in LEVELS if lv in compact), None)
    today = date.today()

    start = expire = None
    for i, line in enumerate(lines):
        ctx = (lines[i - 1] if i else "") + line   # 关键词和日期可能被识别成两行
        ds = _dates_in(line)
        if not ds:
            continue
        if start is None and any(k in ctx for k in START_KW) and not any(k in ctx for k in EXPIRE_KW):
            start = ds[0]
        if expire is None and any(k in ctx for k in EXPIRE_KW):
            expire = ds[0]
    if start is None and expire is not None:
        start = expire - timedelta(days=30)          # 只看到“到期时间”时，按一个月倒推
    if start is None:
        past = [d for d in _dates_in(full) if d <= today]
        start = max(past) if past else None
    date_from = "截图" if start else "今天"
    if start is None or start > today or start < today - timedelta(days=400):
        start, date_from = today, "今天"

    return {
        "has_streamer": has_streamer,
        "level": level,
        "start_date": start.isoformat(),
        "date_from": date_from,          # "截图" = 从截图里读到了日期；"今天" = 没读到
        "uids": UID_RE.findall(full),
        "text": full,
        "compact": compact,
    }


async def read_shot(qq, img: bytes):
    """保存截图并 OCR。返回 (info, 文件名, hash, 错误提示)"""
    h = hashlib.sha256(img).hexdigest()
    if db.hash_used_by_other(h, qq):
        return None, None, h, "这张截图已经被其他QQ号使用过，请发送你自己的上舰截图。"
    path = SHOT_DIR / f"{qq}_{datetime.now():%Y%m%d%H%M%S%f}.png"
    path.write_bytes(img)
    lines = await asyncio.to_thread(_ocr, path)
    return analyze(lines), path.name, h, None


def _drop(name):
    if name:
        (SHOT_DIR / name).unlink(missing_ok=True)


def _match_api(info, guards: dict):
    for uid in info["uids"]:
        if uid in guards:
            return guards[uid]
    # 先把主播名从文字里去掉，避免匹配到名字里带主播名的观众
    text = info["compact"]
    for k in sorted([STREAMER.get("name", "")] + list(STREAMER.get("keywords", [])), key=len, reverse=True):
        if k:
            text = text.replace(k.replace(" ", ""), "\0")
    # 名字越长越可信，优先匹配长名字，避免短名字误命中
    for g in sorted(guards.values(), key=lambda g: -len(g["name"])):
        n = g["name"].replace(" ", "")
        if len(n) >= 2 and n in text:
            return g
    return None


async def verify_screenshot(qq, img: bytes) -> tuple[str, str, dict | None]:
    """群里/私聊直接发截图。返回 (结果, 回复文字, 舰长记录)；结果: verified / pending / rejected / not_guard"""
    info, name, h, err = await read_shot(qq, img)
    if err:
        return "rejected", err, None

    if not info["level"] or not info["has_streamer"]:
        _drop(name)
        miss = "舰长/提督/总督字样" if not info["level"] else f"主播「{STREAMER['name']}」的名字"
        return "not_guard", f"没能在截图里识别到{miss}，请发送清晰、完整的上舰截图（需包含主播名和开通时间）。", None

    month = info["start_date"][:7]
    common = dict(screenshot=name, shot_hash=h, ocr_text=info["text"][:2000])
    matched = None
    if BILI.get("check_api", True):
        guards = await bili.guard_list()
        matched = _match_api(info, guards) if guards else None
        if matched and db.uid_used_by_other(matched["uid"], qq, month):
            return "rejected", "该B站账号本月已经被其他QQ号验证过了，如有疑问请联系主播。", None

    if matched:
        g = db.upsert_guard(qq, month, info["start_date"], "verified", bili_uid=matched["uid"],
                            bili_name=matched["name"], guard_level=matched["level"],
                            source="api", **common)
        return "verified", (f"验证通过！{matched['name']}（{matched['level']}）\n"
                            f"上舰日期：{g['start_date']}（取自{info['date_from']}）"), g

    g = db.upsert_guard(qq, month, info["start_date"], "pending", guard_level=info["level"],
                        bili_uid=info["uids"][0] if info["uids"] else None,
                        source="screenshot", note="截图识别通过，但未在B站在舰列表中匹配到", **common)
    if g["status"] == "verified":
        return "verified", f"你本月已经验证过了（上舰日期 {g['start_date']}）。", g
    return "pending", "截图已收到，但暂时没在直播间舰长列表里匹配到你的B站账号。", g


async def dialog_shot(qq, img: bytes, st: dict) -> tuple[str, str, dict | None]:
    """私聊问答中，已经问到 UID 后对方发来的截图。
    返回 (结果, 文字, 记录)；结果: verified / pending / retry / nodate / old"""
    info, name, h, err = await read_shot(qq, img)
    if err:
        return "retry", err, None
    if not info["level"]:
        _drop(name)
        return "retry", "截图里没识别到「舰长/提督/总督」字样，请换一张更清晰、完整的截图。", None
    if not info["has_streamer"]:
        _drop(name)
        return "retry", f"截图里没看到主播「{STREAMER['name']}」的名字，请发送本直播间的上舰截图。", None
    if info["date_from"] != "截图":
        _drop(name)
        return "nodate", "截图里没找到开通时间或到期时间。请换一张能看到时间的截图，或者回复「没有」。", None

    start = info["start_date"]
    if start[:7] != db.this_month():
        _drop(name)
        return "old", f"截图显示的上舰日期是 {start}，不是本月。", None

    uid, gname = st.get("uid"), st.get("name")
    name_hit = (uid in info["uids"]) or (gname and gname.replace(" ", "") in info["compact"])
    common = dict(bili_uid=uid, bili_name=gname, guard_level=st.get("level") or info["level"],
                  screenshot=name, shot_hash=h, ocr_text=info["text"][:2000])
    if st.get("in_list"):
        g = db.upsert_guard(qq, start[:7], start, "verified", source="dialog",
                            note=f"私聊问答：UID在舰长列表 + 本月截图{'（截图含本人昵称）' if name_hit else ''}",
                            **common)
        return "verified", "", g
    g = db.upsert_guard(qq, start[:7], start, "pending", source="dialog",
                        note="私聊问答：本月截图，但UID不在当前舰长列表", **common)
    return "pending", "", g


async def bind_uid(qq, uid: str) -> tuple[bool, str]:
    """群里「绑定 UID」：在在舰列表里就直接通过"""
    guards = await bili.guard_list()
    if guards is not None and uid not in guards:
        guards = await bili.guard_list(force=True)     # 刚上舰的人缓存里可能还没有
    if guards is None:
        return False, "暂时无法连接B站，请稍后再试或等待主播人工审核。"
    g = guards.get(uid)
    if not g:
        return False, "该UID目前不在本直播间的舰长列表中（刚上舰可能有几分钟延迟，请稍后再试）。"
    month = db.this_month()
    # 必须先发过本月的上舰截图，防止有人直接冒用别人的 UID
    rec = next((r for r in db.guards_of_qq(qq) if r["month"] == month and r["status"] != "rejected"), None)
    if not rec:
        return False, "请先发送你本月的上舰截图，再绑定UID。"
    if db.uid_used_by_other(uid, qq, month):
        return False, "该B站账号本月已经被其他QQ号验证过了，如有疑问请联系主播。"
    db.upsert_guard(qq, month, rec["start_date"], "verified", bili_uid=uid, bili_name=g["name"],
                    guard_level=g["level"], source="api", note="UID绑定核验")
    return True, f"验证通过！{g['name']}（{g['level']}），上舰日期：{rec['start_date']}"
