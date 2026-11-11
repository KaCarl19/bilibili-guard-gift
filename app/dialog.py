"""私聊对话：识别“改地址/手机号”意图，并通过多轮提问确认对方是否本月上舰。

不依赖 QQ，输入 (qq, 文字, 图片列表)，输出要回复的消息，方便单独测试。
流程：
  识别到改地址意图
    ├─ 本月已验证 → 直接发填写链接
    └─ 未验证 → ① 问B站UID → 查直播间舰长列表
                 ② 要本月上舰截图（看开通/到期时间）
                    └─ 没有截图 → ③ 问几号上的舰 → 交给主播审核
"""
import re
import time
from datetime import date, timedelta

from . import bili, db, verify
from .config import STREAMER, WEB

TIMEOUT = 15 * 60              # 15 分钟不回复就结束对话
MAX_TRIES = 3

_states: dict[str, dict] = {}
_last_help: dict[str, float] = {}

# ---------------- 意图识别 ----------------
CANCEL = {"取消", "算了", "不用了", "退出", "不改了", "没事了", "没事"}
YES = re.compile(r"^(是|是的|对|对的|嗯|嗯嗯|没错|就是|yes|y|ok|好)[!！。~～]*$", re.I)
NO_SHOT = re.compile(r"(没有|没截|没图|找不到|不会截|截不了|删了|无)")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ADDR_HINT = re.compile(r"(省|市|区|县|镇|乡|街道|路|街|巷|弄|号|栋|幢|单元|室|村|小区|大厦|公寓|花园)")
_VERB = r"(改|换|更改|修改|更换|更新|变更|填|写|留|补|提交|登记|弄错|填错|写错|搞错|不对|错了|搬家|变了|发一下|告诉)"
_TARGET = r"(地址|收货|收件|手机|电话|号码|联系方式|快递|邮寄|寄到|寄给)"
INTENT = [
    re.compile(_VERB + r".{0,10}" + _TARGET),
    re.compile(_TARGET + r".{0,10}" + _VERB),
    re.compile(r"(填地址|改地址|收货信息|收货地址|收件信息|舰长礼物|上舰礼物|礼物.{0,6}(寄|发|领))"),
]


def classify(text: str) -> str:
    """返回 cancel / change / other"""
    t = re.sub(r"\s", "", text or "")
    if not t:
        return "other"
    if t.strip("。.!！~～") in CANCEL:
        return "cancel"
    if any(p.search(t) for p in INTENT):
        return "change"
    # 直接把新手机号或地址发过来了，也当成改地址
    if PHONE.search(t) or (len(t) >= 10 and len(ADDR_HINT.findall(t)) >= 2):
        return "change"
    return "other"


def extract_draft(text: str) -> dict:
    """从消息里尽量抠出收件人/手机/地址，用来预填网页（最终由本人在网页上确认）"""
    d = {}
    if m := PHONE.search(text):
        d["phone"] = m.group()
    if m := re.search(r"(收件人|姓名|名字)\s*[:：是]?\s*([一-龥A-Za-z·]{2,10})", text):
        d["receiver"] = m.group(2)
    if m := re.search(r"地址\s*[:：是改为成]*\s*([^\n，,；;]{6,120})", text):
        d["address"] = PHONE.sub("", m.group(1)).strip()
    else:
        for line in re.split(r"[\n，,；;]", text):
            line = PHONE.sub("", line).strip()
            if len(line) >= 8 and len(ADDR_HINT.findall(line)) >= 2:
                d["address"] = line
                break
    return d


def parse_day(text: str) -> date | None:
    """解析“11月3日 / 11-3 / 3号 / 今天 / 昨天 / 2026-11-03”"""
    t = re.sub(r"\s", "", text)
    today = date.today()
    for w, n in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if w in t:
            return today - timedelta(days=n)
    try:
        if m := re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.search(r"(\d{1,2})[月/\-.](\d{1,2})", t):
            return date(today.year, int(m[1]), int(m[2]))
        if m := re.search(r"(\d{1,2})[日号]", t):
            return date(today.year, today.month, int(m[1]))
    except ValueError:
        return None
    return None


# ---------------- 输出 ----------------
class Out:
    def __init__(self):
        self.msgs: list[str] = []      # 回复给对方
        self.admin: list[str] = []     # 通知主播/管理员

    def say(self, s):
        self.msgs.append(s)


def help_text():
    return (f"你好，这里是「{STREAMER['name']}」的舰长助手～\n"
            "· 填写/修改舰长礼物的收货地址、手机号：发「改地址」\n"
            "· 查看已登记的信息：发「我的信息」\n"
            "· 其他问题请联系主播哦")


def _send_link(o: Out, qq: str, g: dict):
    hours = int(WEB.get("link_valid_hours", 24))
    link = f"{WEB['public_url'].rstrip('/')}/fill?t={db.new_token(qq, hours)}"
    verb = "修改" if db.get_address(qq) else "填写"
    o.say(f"✅ 已确认你是本月的{g.get('guard_level') or '舰长'}（上舰日期 {g['start_date']}）。\n"
          f"请打开下面的链接{verb}收件人、手机号和地址（{hours}小时内有效，请勿转发）：\n{link}")
    if qq in db.DRAFTS:
        o.say("你刚才发的收货信息已经预填在页面里了，核对无误后点「保存」才会生效。")


def _ask_uid_text():
    return "① 请告诉我你的B站UID（B站App → 我的 → 点头像进入个人空间，能看到 UID）"


def _ask_shot_text(prefix):
    return (prefix + "还需要确认你是本月上的舰：请发一张能看到「开通时间」或「到期时间」的截图"
            "（B站App → 我的 → 我的大航海，或者开通记录页面）。\n没有截图的话回复「没有」。")


# ---------------- 主入口 ----------------
async def handle(qq, text: str = "", images: list[bytes] | None = None) -> Out:
    qq, text, images = str(qq), (text or "").strip(), images or []
    o = Out()
    now = time.time()
    for k in [k for k, s in _states.items() if now - s["t"] > TIMEOUT]:
        _states.pop(k, None)

    st = _states.get(qq)
    kind = classify(text)

    if kind == "cancel":
        if st:
            _states.pop(qq, None)
            o.say("好的，已取消。之后需要修改时再发「改地址」就行～")
        return o

    # 「绑定 12345」 直接进入 UID 核对
    if m := re.match(r"^/?绑定\s*(\d{3,16})$", text):
        st = _states[qq] = {"step": "uid", "t": now, "tries": 0}
        return await _step(o, qq, st, m[1], [])

    if st:
        st["t"] = now
        if kind == "change" and not images:
            _save_draft(qq, text)
        return await _step(o, qq, st, text, images)

    if images:
        return await _direct_shot(o, qq, images)

    if kind == "change":
        _save_draft(qq, text)
        g = db.editable_guard(qq)
        if g:
            _send_link(o, qq, g)
            return o
        st = _states[qq] = {"step": "uid", "t": now, "tries": 0}
        o.say(f"收到～你是想填写/修改舰长礼物的收货信息对吧？\n"
              f"按规则只有本月（{date.today().month}月）上舰的舰长才能填写或修改，"
              f"所以我先问你几个问题确认一下，随时发「取消」可以退出。")
        known = next((r for r in db.guards_of_qq(qq) if r.get("bili_uid")), None)
        if known:
            st["known_uid"] = known["bili_uid"]
            o.say(f"① 你之前登记的B站账号是 {known['bili_name'] or ''}（UID {known['bili_uid']}），"
                  f"还是这个账号吗？是的话回复「是」，不是请直接发新的UID。")
        else:
            o.say(_ask_uid_text())
        return o

    if now - _last_help.get(qq, 0) > 6 * 3600:      # 无关消息：6 小时内只提示一次，避免刷屏
        _last_help[qq] = now
        o.say(help_text())
    return o


def _save_draft(qq, text):
    d = extract_draft(text)
    if d:
        db.DRAFTS.setdefault(qq, {}).update(d)


async def _direct_shot(o: Out, qq: str, images):
    """没在对话里，直接私聊发了截图"""
    for img in images:
        result, text, g = await verify.verify_screenshot(qq, img)
        if result == "verified":
            e = db.editable_guard(qq)
            if e:
                _send_link(o, qq, e)
            else:
                o.say(text + "\n不过这次上舰不是本月的，按规则暂时不能填写/修改地址。")
            return o
        if result == "pending":
            # 截图像是真的但对不上账号：转入问答，问 UID
            _states[qq] = {"step": "uid", "t": time.time(), "tries": 0}
            o.say(text + "\n" + _ask_uid_text())
            return o
        o.say(text)
    return o


async def _step(o: Out, qq: str, st: dict, text: str, images):
    step = st["step"]

    # ---------- ① 问 UID ----------
    if step == "uid":
        uid = None
        if st.get("known_uid") and YES.match(text):
            uid = st["known_uid"]
        elif m := re.search(r"(?<!\d)(\d{3,16})(?!\d)", text):
            uid = m[1]
        if not uid:
            return _retry(o, qq, st, "没看懂～请直接发送你的B站UID（一串数字，例如 12345678）。")
        st.update(uid=uid, tries=0)

        guards = await bili.guard_list()
        if guards is not None and uid not in guards:
            guards = await bili.guard_list(force=True)
        month = db.this_month()

        if guards is None:
            st.update(in_list=None, step="shot")
            o.say(_ask_shot_text("暂时连不上B站，没法自动核对舰长列表。\n② "))
            return o
        if uid not in guards:
            st.update(in_list=False, step="shot")
            o.say(f"❗ UID {uid} 目前不在「{STREAMER['name']}」直播间的舰长列表里。\n"
                  "· 刚上舰的话列表可能有几分钟延迟，可以过几分钟再发一次UID；\n"
                  "· 如果确实本月上过舰，请发一张能看到「开通时间」的上舰截图，我会交给主播人工核对"
                  "（没有截图就回复「没有」）；\n"
                  "· 发错了或不是舰长，发「取消」即可。")
            return o
        g = guards[uid]
        if db.uid_used_by_other(uid, qq, month):
            _states.pop(qq, None)
            o.say("这个B站账号本月已经和另一个QQ绑定了。如果你换了QQ号，请联系主播处理。")
            o.admin.append(f"⚠️ QQ {qq} 想用B站账号 {g['name']}（UID {uid}）改地址，但该账号本月已绑定其他QQ，请留意。")
            return o
        st.update(in_list=True, name=g["name"], level=g["level"])

        # 本月已经发过带日期的截图、只是没对上账号 → 现在对上了，直接通过
        rec = next((r for r in db.guards_of_qq(qq)
                    if r["month"] == month and r["status"] == "pending" and r["screenshot"]), None)
        if rec:
            g2 = db.upsert_guard(qq, month, rec["start_date"], "verified", bili_uid=uid,
                                 bili_name=g["name"], guard_level=g["level"], source="dialog",
                                 note="私聊问答：截图 + UID在舰长列表")
            _states.pop(qq, None)
            _send_link(o, qq, g2)
            return o

        st["step"] = "shot"
        o.say(_ask_shot_text(f"② 找到了：{g['name']}（{g['level']}），目前在舰 ✔\n"))
        return o

    # ---------- ② 要截图 ----------
    if step == "shot":
        if images:
            result, msg, g = await verify.dialog_shot(qq, images[0], st)
            if result == "verified":
                _states.pop(qq, None)
                _send_link(o, qq, g)
            elif result == "pending":
                _states.pop(qq, None)
                o.say(f"截图已收到，上舰日期 {g['start_date']}。因为你的UID暂时不在直播间舰长列表里，"
                      "已经交给主播人工审核，审核通过后我会通知你，到时再发「改地址」即可。")
                o.admin.append(f"📝 待审核：QQ {qq}（UID {st.get('uid')}）通过私聊提交了本月上舰截图，"
                               f"自称 {g['start_date']} 上舰，但UID不在舰长列表。请到后台核对。")
            elif result == "old":
                _states.pop(qq, None)
                o.say(msg + f"\n按规则只有本月上舰的舰长才能填写/修改地址。如果你本月有续舰，"
                            "请发续舰后的截图；有疑问请联系主播。")
            else:   # retry / nodate
                return _retry(o, qq, st, msg)
            return o
        if NO_SHOT.search(text):
            st.update(step="date", tries=0)
            o.say("③ 好的，那请告诉我你是本月几号上的舰？（例如：11月3日）")
            return o
        return _retry(o, qq, st, "请发送上舰截图，或者回复「没有」。")

    # ---------- ③ 问日期（无截图） ----------
    if step == "date":
        d = parse_day(text)
        if not d:
            return _retry(o, qq, st, "没看懂日期～请按「11月3日」这样的格式回复。")
        if d > date.today():
            return _retry(o, qq, st, "日期不能晚于今天哦，请再确认一下。")
        _states.pop(qq, None)
        if d.strftime("%Y-%m") != db.this_month():
            o.say(f"你说的是 {d:%Y年%m月%d日}，不是本月。按规则只有本月上舰的舰长才能填写/修改地址，"
                  "本月续舰后再来找我就行～有疑问请联系主播。")
            return o
        in_list = st.get("in_list")
        g = db.upsert_guard(qq, d.strftime("%Y-%m"), d.isoformat(), "pending", bili_uid=st.get("uid"),
                            bili_name=st.get("name"), guard_level=st.get("level"), source="dialog",
                            note=f"私聊问答：无截图，自述 {d} 上舰；UID{'在' if in_list else '不在'}舰长列表")
        o.say("好的，已经记录下来并交给主播核对（没有截图需要人工确认），审核通过后我会通知你，"
              "到时再发「改地址」即可。")
        o.admin.append(f"📝 待审核：QQ {qq}（{st.get('name') or ''} UID {st.get('uid')}）自述 {d} 上舰，"
                       f"无截图，UID{'在' if in_list else '不在'}舰长列表。请到后台核对。")
        return o

    _states.pop(qq, None)
    return o


def _retry(o: Out, qq, st, msg):
    st["tries"] = st.get("tries", 0) + 1
    if st["tries"] >= MAX_TRIES:
        _states.pop(qq, None)
        o.say("好像一直没对上😥 这次先结束了。需要时再发「改地址」重新开始，或者直接联系主播。")
    else:
        o.say(msg)
    return o
