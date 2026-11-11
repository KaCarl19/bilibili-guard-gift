"""网页部分：观众填写/修改地址 + 主播后台"""
import asyncio
import csv
import io
import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db, notify
from .config import ROOT, SHOT_DIR, STREAMER, WEB

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
security = HTTPBasic()

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
STATUS_CN = {"verified": "已验证", "pending": "待审核", "rejected": "已拒绝"}


def admin(cred: HTTPBasicCredentials = Depends(security)):
    if not secrets.compare_digest(cred.password.encode(), str(WEB["admin_password"]).encode()):
        raise HTTPException(401, "密码错误", headers={"WWW-Authenticate": "Basic"})
    return True


def _page(request, name, **ctx):
    ctx.setdefault("streamer", STREAMER["name"])
    return templates.TemplateResponse(request, name, ctx)


# ======================= 观众填写 =======================
def _check_token(t):
    qq = db.qq_of_token(t or "")
    if not qq:
        return None, None, "链接无效或已过期，请私聊机器人发送「填地址」重新获取。"
    g = db.editable_guard(qq)
    if not g:
        return qq, None, "系统查询到你不是本月已验证的舰长，暂时不能填写或修改地址。如有疑问请联系主播。"
    return qq, g, None


@router.get("/fill")
async def fill_page(request: Request, t: str = ""):
    qq, g, err = _check_token(t)
    if err:
        return _page(request, "message.html", title="无法填写", msg=err)
    addr = dict(db.get_address(qq) or {})
    draft = db.DRAFTS.get(qq)
    if draft:
        addr.update(draft)
    return _page(request, "fill.html", t=t, guard=g, addr=addr, errors=[], draft=bool(draft))


@router.post("/fill")
async def fill_submit(request: Request, t: str = Form(""), receiver: str = Form(""),
                      phone: str = Form(""), address: str = Form("")):
    qq, g, err = _check_token(t)          # 提交时再查一次，防止链接留到下个月再改
    if err:
        return _page(request, "message.html", title="无法提交", msg=err)
    receiver, phone, address = receiver.strip(), re.sub(r"\D", "", phone), address.strip()
    errors = []
    if not (1 <= len(receiver) <= 20):
        errors.append("请填写收件人姓名")
    if not PHONE_RE.match(phone):
        errors.append("手机号格式不正确（需11位大陆手机号）")
    if not (8 <= len(address) <= 200):
        errors.append("请填写完整的收货地址（省市区 + 详细地址）")
    if errors:
        return _page(request, "fill.html", t=t, guard=g, errors=errors,
                     addr={"receiver": receiver, "phone": phone, "address": address})
    first = db.get_address(qq) is None
    db.save_address(qq, receiver, phone, address)
    return _page(request, "message.html", title="提交成功",
                 msg=("地址已保存，礼物会寄到这里，感谢上舰！" if first else "地址已更新！")
                 + " 如需再次修改，重新打开链接即可。", ok=True)


# ======================= 主播后台 =======================
@router.get("/admin")
async def admin_page(request: Request, month: str = "", status: str = "", _=Depends(admin)):
    month = month or db.this_month()
    rows = db.list_guards(month if month != "all" else None, status or None)
    months = sorted(set(db.all_months()) | {db.this_month()}, reverse=True)
    stat = {
        "total": len(rows),
        "verified": sum(r["status"] == "verified" for r in rows),
        "pending": sum(r["status"] == "pending" for r in rows),
        "no_addr": sum(r["status"] == "verified" and not r["address"] for r in rows),
    }
    return _page(request, "admin.html", rows=rows, months=months, month=month, status=status,
                 stat=stat, status_cn=STATUS_CN, today=date.today().isoformat())


@router.post("/admin/guard/{gid}")
async def admin_guard_action(gid: int, action: str = Form(...), back: str = Form("/admin"),
                             _=Depends(admin)):
    g = db.get_guard(gid)
    if action == "approve":
        db.set_guard_status(gid, "verified", "主播人工审核通过")
        if g and g["month"] == db.this_month():
            asyncio.create_task(notify.send_private(
                g["qq"], f"🎉 你的舰长身份（{g['start_date']} 上舰）已通过主播审核！发送「改地址」即可填写收货信息。"))
    elif action == "reject":
        db.set_guard_status(gid, "rejected", "主播人工拒绝")
        if g:
            asyncio.create_task(notify.send_private(
                g["qq"], "很抱歉，你提交的舰长信息没有通过主播审核。如有疑问请直接联系主播。"))
    elif action == "ship":
        db.set_shipped(gid, True)
    elif action == "unship":
        db.set_shipped(gid, False)
    elif action == "delete":
        db.delete_guard(gid)
    return RedirectResponse(back if back.startswith("/admin") else "/admin", status_code=303)


@router.post("/admin/add")
async def admin_add(qq: str = Form(...), start_date: str = Form(...), bili_name: str = Form(""),
                    bili_uid: str = Form(""), guard_level: str = Form("舰长"), _=Depends(admin)):
    qq = re.sub(r"\D", "", qq)
    if not qq:
        raise HTTPException(400, "QQ号不能为空")
    date.fromisoformat(start_date)
    db.upsert_guard(qq, start_date[:7], start_date, "verified", bili_uid=bili_uid or None,
                    bili_name=bili_name or None, guard_level=guard_level, source="manual",
                    note="主播手动添加")
    return RedirectResponse(f"/admin?month={start_date[:7]}", status_code=303)


@router.post("/admin/address")
async def admin_address(qq: str = Form(...), receiver: str = Form(...), phone: str = Form(...),
                        address: str = Form(...), back: str = Form("/admin"), _=Depends(admin)):
    db.save_address(qq, receiver.strip(), re.sub(r"\D", "", phone), address.strip())
    return RedirectResponse(back if back.startswith("/admin") else "/admin", status_code=303)


@router.get("/admin/export.csv")
async def admin_export(month: str = "", _=Depends(admin)):
    month = month or db.this_month()
    rows = db.list_guards(month if month != "all" else None, "verified")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["月份", "上舰日期", "等级", "B站昵称", "B站UID", "QQ", "收件人", "手机号", "收货地址", "已发货"])
    for r in rows:
        w.writerow([r["month"], r["start_date"], r["guard_level"], r["bili_name"] or "", r["bili_uid"] or "",
                    r["qq"], r["receiver"] or "", r["phone"] or "", r["address"] or "",
                    "是" if r["shipped"] else "否"])
    data = "﻿" + buf.getvalue()           # 带 BOM，Excel 直接打开不乱码
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=guards_{month}.csv"})


@router.get("/admin/shot/{name}")
async def admin_shot(name: str, _=Depends(admin)):
    p = (SHOT_DIR / name).resolve()
    if p.parent != SHOT_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@router.get("/")
async def index():
    return RedirectResponse("/admin")
