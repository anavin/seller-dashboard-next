"""
Vercel Python serverless function: /api/data
ดึงข้อมูล Lazada สด -> คืน JSON ให้หน้า dashboard (เช็ครหัสผ่านก่อน)

Environment Variables (Vercel → Settings → Environment Variables):
    DASH_PASSWORD, LZ_APP_KEY, LZ_APP_SECRET, LZ_REFRESH_TOKEN
    LOOKBACK_DAYS (ออปชัน, ค่าเริ่มต้น 120) = ดึงย้อนหลังกี่วัน

ดูข้อมูลดิบเพื่อ debug: เปิด /api/data?pw=<รหัส>&debug=1
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import time
import hmac
import hashlib
import datetime
import urllib.parse

import requests

LAZADA_BASE = "https://api.lazada.co.th/rest"
AUTH_BASE = "https://auth.lazada.com/rest"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "730"))
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _sb_all(table, select="*", extra=""):
    """อ่านทุกแถวจาก Supabase แบบแบ่งหน้า"""
    out, offset = [], 0
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    while True:
        url = f"{SB_URL}/rest/v1/{table}?select={select}&limit=1000&offset={offset}{extra}"
        r = requests.get(url, headers=h, timeout=25)
        r.raise_for_status()
        rows = r.json()
        out += rows
        if len(rows) < 1000:
            break
        offset += 1000
    return out


def _pay_group(p):
    """จัดกลุ่มวิธีชำระเงิน (ละเอียด -> หมวดใหญ่อ่านง่าย) ทำที่หลังบ้านก่อนส่งให้ dashboard"""
    s = str(p or "").upper().strip()
    if not s:
        return "ไม่ระบุ"
    if "COD" in s:
        return "เก็บปลายทาง (COD)"
    if "PROMPTPAY" in s:
        return "PromptPay"
    if "VIRTUAL_ACCOUNT" in s or "_BANK" in s or "BANK_VA" in s or "BANK_ONLINE" in s:
        return "โอนผ่านธนาคาร"
    if "CARD" in s or "PAY_LATER" in s or "CREDIT" in s or "DEBIT" in s or "INSTALLMENT" in s:
        return "บัตร/ผ่อน"
    if "TMN" in s or "WALLET" in s or "LINE_PAY" in s or "RABBIT" in s or "PAYMENT_ACCOUNT" in s:
        return "e-Wallet"
    if "OTC" in s or "SEVENELEVEN" in s or "COUNTER" in s:
        return "เคาน์เตอร์/ร้านสะดวกซื้อ"
    if "ZERO" in s or "FREE" in s:
        return "ฟรี/0 บาท"
    return "อื่นๆ"


def build_from_db():
    """อ่านข้อมูลจาก Supabase แล้วประกอบเป็น JSON เดียวกับที่ dashboard ใช้ (เร็ว)"""
    orders = _sb_all("lz_orders")
    items = _sb_all("lz_order_items")
    products = _sb_all("lz_products")

    by_order = {}
    for it in items:
        by_order.setdefault(it["order_id"], []).append({
            "sku": it.get("sku", ""), "name": it.get("name", ""),
            "category": it.get("category", "") or "",
            "qty": int(it.get("qty", 1) or 1),
            "price": float(it.get("price", 0) or 0), "cost": float(it.get("cost", 0) or 0),
        })

    order_rows = [{
        "date": str(o.get("date", ""))[:10], "hour": int(o.get("hour", 12) or 12),
        "platform": o.get("platform", "lazada"), "status": o.get("status", ""),
        "region": o.get("region", "") or "", "customer": o.get("customer", "new"),
        "shipping_fee": float(o.get("shipping_fee", 0) or 0),
        "platform_fee": float(o.get("platform_fee", 0) or 0),
        "buyer": o.get("buyer_key", "") or "",
        "payment": _pay_group(o.get("payment_method", "")),
        "items": by_order.get(o["order_id"], []),
    } for o in orders]

    prod_rows = [{
        "sku": p.get("sku", ""), "name": p.get("name", ""), "category": p.get("category", "") or "",
        "price": float(p.get("price", 0) or 0), "cost": float(p.get("cost", 0) or 0),
        "stock": {"tiktok": 0, "shopee": 0, "lazada": int(p.get("stock_lazada", 0) or 0)},
    } for p in products]

    dates = sorted({r["date"] for r in order_rows if r["date"]})
    cats = sorted({i["category"] for r in order_rows for i in r["items"] if i["category"]})
    regions = sorted({r["region"] for r in order_rows if r["region"]})
    last_sync = None
    try:
        sy = _sb_all("lz_sync", "last_sync_time")
        last_sync = sy[0].get("last_sync_time") if sy else None
    except Exception:
        last_sync = None
    # การเงิน: ใบสรุปยอดโอน (ถ้ามี)
    finance = []
    try:
        for r in _sb_all("lz_finance"):
            finance.append({
                "statement": r.get("statement_number", ""),
                "date": str(r.get("date", ""))[:10],
                "revenue": float(r.get("item_revenue", 0) or 0),
                "fees": float(r.get("fees_total", 0) or 0),
                "refunds": float(r.get("refunds", 0) or 0),
                "payout": float(r.get("payout", 0) or 0),
            })
    except Exception:
        finance = []
    reviews = []
    try:
        for r in _sb_all("lz_reviews"):
            reviews.append({
                "item_id": r.get("item_id", ""), "sku": r.get("sku", ""),
                "name": r.get("name", "") or r.get("sku", ""),
                "rating": int(r.get("rating", 0) or 0),
                "content": r.get("content", "") or "",
                "time": str(r.get("review_time", "") or "")[:10],
                "reply": bool(r.get("has_reply")),
            })
    except Exception:
        reviews = []
    return {
        "meta": {"currency": "THB",
                 "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "start_date": dates[0] if dates else "", "end_date": dates[-1] if dates else "",
                 "days": len(dates), "source": "supabase", "last_sync": last_sync,
                 "platforms": ["tiktok", "shopee", "lazada"], "categories": cats, "regions": regions},
        "orders": order_rows, "products": prod_rows, "ads": [], "finance": finance, "reviews": reviews,
    }


def _sign(secret, path, params):
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), (path + ordered).encode(),
                    hashlib.sha256).hexdigest().upper()


def _call(base, path, app_key, app_secret, access_token, extra=None):
    params = {"app_key": app_key, "timestamp": str(int(time.time() * 1000)),
              "sign_method": "sha256"}
    if access_token:
        params["access_token"] = access_token
    if extra:
        params.update({k: str(v) for k, v in extra.items()})
    params["sign"] = _sign(app_secret, path, params)
    r = requests.get(base + path, params=params, timeout=25)
    r.raise_for_status()
    d = r.json()
    if str(d.get("code", "0")) not in ("0", ""):
        raise RuntimeError(f"Lazada error [{path}]: {d.get('code')} {d.get('message')}")
    return d


def _call_soft(base, path, app_key, app_secret, access, extra=None):
    """เรียก API แบบไม่พัง — ถ้า error คืน None (ใช้กับข้อมูลเสริม)"""
    try:
        return _call(base, path, app_key, app_secret, access, extra)
    except Exception:
        return None


def order_sample():
    """ดูว่า Lazada ส่งฟิลด์ผู้ซื้อ/ที่อยู่อะไรมาบ้าง (ประเมินว่าทำ repeat-customer ได้ไหม)"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    rt = None
    try:
        sy = _sb_all("lz_sync", "refresh_token")
        rt = sy[0].get("refresh_token") if sy else None
    except Exception:
        rt = None
    rt = rt or os.environ.get("LZ_REFRESH_TOKEN", "")
    access = _refresh(app_key, app_secret, rt)
    now = datetime.datetime.now().astimezone()
    since = (now - datetime.timedelta(days=30)).replace(microsecond=0).isoformat()
    d = _call(LAZADA_BASE, "/orders/get", app_key, app_secret, access,
              {"created_after": since, "limit": 3, "sort_direction": "DESC"})
    orders = (d.get("data") or {}).get("orders") or []
    out = []
    for o in orders[:3]:
        out.append({
            "order_id": o.get("order_id"),
            "customer_first_name": o.get("customer_first_name"),
            "customer_last_name": o.get("customer_last_name"),
            "address_shipping": o.get("address_shipping"),
            "all_order_keys": sorted(o.keys()),
        })
    return {"sample_orders": out,
            "หมายเหตุ": "ดูว่ามี postcode/phone/address1 ที่พอใช้ทำลายนิ้วมือผู้ซื้อได้ไหม"}


def api_probe():
    """ยิงทดสอบหลาย endpoint (รีวิว/คืนสินค้า/ไทม์ไลน์จัดส่ง ฯลฯ) ดูว่าอันไหนเปิดสิทธิ์ + มีข้อมูล"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    rt = None
    try:
        sy = _sb_all("lz_sync", "refresh_token")
        rt = sy[0].get("refresh_token") if sy else None
    except Exception:
        rt = None
    rt = rt or os.environ.get("LZ_REFRESH_TOKEN", "")
    access = _refresh(app_key, app_secret, rt)
    now = datetime.datetime.now().astimezone()
    # ออเดอร์เก่า ~50 วัน (น่าจะส่งถึงแล้ว มี tracking)
    oid = None
    try:
        a = (now - datetime.timedelta(days=55)).replace(microsecond=0).isoformat()
        b = (now - datetime.timedelta(days=45)).replace(microsecond=0).isoformat()
        d = _call(LAZADA_BASE, "/orders/get", app_key, app_secret, access,
                  {"created_after": a, "created_before": b, "limit": 1, "sort_direction": "DESC"})
        orders = (d.get("data") or {}).get("orders") or []
        if orders:
            oid = str(orders[0].get("order_id"))
    except Exception:
        oid = None
    ms_a = int((now - datetime.timedelta(days=180)).timestamp() * 1000)
    ms_b = int(now.timestamp() * 1000)
    sec_a = int((now - datetime.timedelta(days=180)).timestamp())
    sec_b = int(now.timestamp())
    # ดึง item_id หลายตัวมายิง review list v2 (ให้เจอตัวที่มีรีวิว)
    item_ids = []
    try:
        dp = _call(LAZADA_BASE, "/products/get", app_key, app_secret, access, {"limit": 50, "filter": "all"})
        for p in ((dp.get("data") or {}).get("products") or []):
            iid = p.get("item_id")
            if iid:
                item_ids.append(iid)
    except Exception:
        item_ids = []
    cands = [
        ("รีวิว: list v2 (50 สินค้า)", "/review/seller/list/v2",
         {"id_list": json.dumps(item_ids[:50])}),
        ("รีวิว: list v2 (review_type=all)", "/review/seller/list/v2",
         {"id_list": json.dumps(item_ids[:50]), "review_type": "all"}),
        ("รีวิว: history (item แรก)", "/review/seller/history/list",
         {"item_id": item_ids[0] if item_ids else 0, "start_time": ms_a, "end_time": ms_b, "current": 1, "page_size": 20}),
        ("ออเดอร์รายตัว (มี voucher)", "/order/get", ({"order_id": oid} if oid else {})),
    ]
    out = []
    for label, path, params in cands:
        try:
            d = _call(LAZADA_BASE, path, app_key, app_secret, access, params)
            data = d.get("data")
            sample = data
            if isinstance(data, list):
                sample = data[:1]
            elif isinstance(data, dict):
                sample = {k: (v[:1] if isinstance(v, list) else v) for k, v in list(data.items())[:8]}
            out.append({"api": label, "path": path, "ok": True, "code": str(d.get("code", "0")),
                        "message": d.get("message", ""), "has_data": bool(data), "sample": sample})
        except Exception as e:
            out.append({"api": label, "path": path, "ok": False, "result": str(e)})
    return {"order_id_used": oid, "probes": out,
            "หมายเหตุ": "ok+has_data = ใช้ได้ · InvalidApiPath = path ผิด (ลอง path อื่น) · permission/whitelist = ต้องเปิดสิทธิ์"}


def review_sample():
    """ดึงรีวิวจริงมาดูโครงสร้างฟิลด์ (หา item ที่มีรีวิวแล้วคืน raw review ตัวแรก)"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    rt = None
    try:
        sy = _sb_all("lz_sync", "refresh_token")
        rt = sy[0].get("refresh_token") if sy else None
    except Exception:
        rt = None
    rt = rt or os.environ.get("LZ_REFRESH_TOKEN", "")
    access = _refresh(app_key, app_secret, rt)
    item_ids, offset = [], 0
    while True and offset < 2000:
        dp = _call(LAZADA_BASE, "/products/get", app_key, app_secret, access,
                   {"limit": 50, "offset": offset, "filter": "all"})
        batch = (dp.get("data") or {}).get("products") or []
        if not batch:
            break
        for p in batch:
            if p.get("item_id"):
                item_ids.append(p.get("item_id"))
        if len(batch) < 50:
            break
        offset += 50
    now = datetime.datetime.now().astimezone()
    ms_b = int(now.timestamp() * 1000)
    ms_a = int((now - datetime.timedelta(days=30)).timestamp() * 1000)
    checked, last_err = 0, None
    for iid in item_ids[:80]:
        checked += 1
        try:
            d = _call(LAZADA_BASE, "/review/seller/history/list", app_key, app_secret, access,
                      {"item_id": iid, "start_time": ms_a, "end_time": ms_b, "current": 1, "page_size": 20})
        except Exception as e:
            last_err = str(e)
            continue
        data = d.get("data")
        rlist = []
        if isinstance(data, dict):
            rlist = data.get("review_list") or data.get("reviews") or data.get("list") or []
        elif isinstance(data, list):
            rlist = data
        if rlist:
            return {"items_checked": checked, "endpoint": "history",
                    "data_keys": sorted(data.keys()) if isinstance(data, dict) else "list",
                    "first_review_raw": rlist[0], "review_keys": sorted(rlist[0].keys())}
    return {"items_checked": checked, "total_items": len(item_ids), "first_review_raw": None,
            "last_error": last_err, "note": "ยังไม่เจอรีวิวใน 40 item แรกผ่าน history — ดู last_error ว่าติด param ไหน"}


def _refresh(app_key, app_secret, refresh_token):
    d = _call(AUTH_BASE, "/auth/token/refresh", app_key, app_secret, None,
              {"refresh_token": refresh_token})
    if not d.get("access_token"):
        raise RuntimeError(f"refresh failed: {d}")
    return d["access_token"]


def finance_check():
    """ทดสอบว่าแอปเปิดสิทธิ์ Finance/Ads API ไหม — ยิงจริงแล้วดู code/message ที่ Lazada ตอบ"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    rt = None
    try:
        sy = _sb_all("lz_sync", "refresh_token")
        rt = sy[0].get("refresh_token") if sy else None
    except Exception:
        rt = None
    rt = rt or os.environ.get("LZ_REFRESH_TOKEN", "")
    access = _refresh(app_key, app_secret, rt)
    end = datetime.date.today()
    start = end - datetime.timedelta(days=7)
    probes = [
        ("finance: transaction details", "/finance/transaction/details/get",
         {"start_time": start.isoformat(), "end_time": end.isoformat(), "limit": 1, "offset": 0}),
        ("finance: payout status", "/finance/payout/status/get",
         {"created_after": start.isoformat(), "created_before": end.isoformat()}),
    ]
    out = []
    for label, path, params in probes:
        try:
            d = _call(LAZADA_BASE, path, app_key, app_secret, access, params)
            data = d.get("data")
            sample = data
            if isinstance(data, list):
                sample = data[:2]
            elif isinstance(data, dict):
                # ถ้าเป็น dict ที่มี list ข้างใน เอามาโชว์ 2 รายการแรก
                sample = {k: (v[:2] if isinstance(v, list) else v) for k, v in data.items()}
            out.append({"api": label, "path": path, "ok": True,
                        "code": str(d.get("code", "0")), "message": d.get("message", ""),
                        "has_data": bool(data), "sample": sample})
        except Exception as e:
            out.append({"api": label, "path": path, "ok": False, "result": str(e)})
    return {"token_ok": bool(access), "probes": out,
            "หมายเหตุ": "code 0 หรือ has_data=true = เปิดสิทธิ์แล้ว · ถ้าขึ้น IncompleteSignature/MissingParameter = ถึง API ได้ (สิทธิ์น่าจะมี แค่พารามิเตอร์) · ถ้าขึ้น ApiNotInWhiteList/permission/access denied = ยังไม่เปิดสิทธิ์"}


def _hour(s):
    try:
        return int(str(s)[11:13])
    except Exception:
        return 12


def _category_map(app_key, app_secret, access):
    """ดึง category tree -> map id เป็นชื่อหมวดหมู่ (fail-soft)"""
    d = _call_soft(LAZADA_BASE, "/category/tree/get", app_key, app_secret, access)
    idname = {}
    if not d:
        return idname

    def walk(nodes):
        for n in nodes or []:
            cid = n.get("category_id")
            if cid is not None and n.get("name"):
                idname[str(cid)] = n.get("name")
            walk(n.get("children"))
    walk(d.get("data") or [])
    return idname


def _finance_debug(app_key, app_secret, access, start, end):
    """ลองเรียก finance API หลายแบบ เก็บผลดิบไว้ดู (ใช้เฉพาะ debug)"""
    out = {}
    sd, ed = start[:10], end[:10]
    attempts = [
        ("/finance/transaction/details/get", {"start_time": sd, "end_time": ed, "limit": 5, "offset": 0}),
        ("/finance/transaction/accountTransactions/query", {"start_time": sd, "end_time": ed}),
    ]
    for path, extra in attempts:
        try:
            out[path] = _call(LAZADA_BASE, path, app_key, app_secret, access, extra)
        except Exception as e:
            out[path] = str(e)
    return out


def build_data(debug=False):
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    refresh_token = os.environ["LZ_REFRESH_TOKEN"]
    access = _refresh(app_key, app_secret, refresh_token)

    now = datetime.datetime.now().astimezone()
    start = (now - datetime.timedelta(days=LOOKBACK_DAYS)).replace(microsecond=0).isoformat()
    end = now.replace(microsecond=0).isoformat()

    # ---- orders ----
    raw_orders, offset = [], 0
    while True:
        d = _call(LAZADA_BASE, "/orders/get", app_key, app_secret, access,
                  {"created_after": start, "created_before": end,
                   "limit": 100, "offset": offset, "sort_direction": "DESC"})
        batch = d.get("data", {}).get("orders", [])
        raw_orders += batch
        if len(batch) < 100:
            break
        offset += 100
        if offset >= 5000:
            break

    # ---- items (batch ทีละ 50 ออเดอร์) ----
    items_by_order = {}
    ids = [str(o.get("order_id")) for o in raw_orders]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = _call(LAZADA_BASE, "/orders/items/get", app_key, app_secret, access,
                  {"order_ids": json.dumps(chunk)})
        for od in d.get("data", []):
            items_by_order[str(od.get("order_id"))] = od.get("order_items", [])

    order_rows = []
    for o in raw_orders:
        oid = str(o.get("order_id"))
        statuses = o.get("statuses") or ["unknown"]
        its = items_by_order.get(oid, [])
        items = [{
            "sku": it.get("sku") or it.get("shop_sku", ""),
            "name": it.get("name", ""),
            "category": "",
            "qty": 1,
            "price": float(it.get("paid_price") or it.get("item_price") or 0),
            "cost": 0.0,
        } for it in its]
        order_rows.append({
            "date": str(o.get("created_at", ""))[:10],
            "hour": _hour(o.get("created_at", "")),
            "platform": "lazada",
            "status": str(statuses[0]).lower(),
            "region": (o.get("address_shipping") or {}).get("city", "") or "",
            "customer": "new",
            "shipping_fee": float(o.get("shipping_fee", 0) or 0),
            "platform_fee": 0.0,
            "items": items,
        })

    # ---- products + หมวดหมู่จริง ----
    idname = _category_map(app_key, app_secret, access)   # id -> ชื่อหมวด (fail-soft)
    sku_cat = {}
    raw_product_sample = None
    pmap = {}
    offset = 0
    while True:
        d = _call(LAZADA_BASE, "/products/get", app_key, app_secret, access,
                  {"limit": 50, "offset": offset, "filter": "all"})
        batch = d.get("data", {}).get("products", [])
        if not batch:
            break
        if raw_product_sample is None:
            raw_product_sample = batch[0]
        for p in batch:
            name = (p.get("attributes") or {}).get("name", "")
            pcat = idname.get(str(p.get("primary_category") or ""), "")
            for sk in p.get("skus", [{}]):
                sku = sk.get("SellerSku") or sk.get("ShopSku", "")
                rec = pmap.setdefault(sku, {"sku": sku, "name": name, "category": pcat,
                                            "price": float(sk.get("price", 0) or 0),
                                            "cost": 0.0,
                                            "stock": {"tiktok": 0, "shopee": 0, "lazada": 0}})
                rec["category"] = pcat or rec["category"]
                rec["stock"]["lazada"] = int(sk.get("quantity", 0) or 0)
                if pcat:
                    sku_cat[sku] = pcat
        if len(batch) < 50:
            break
        offset += 50
        if offset >= 5000:
            break

    # เติมหมวดหมู่ให้ item ในออเดอร์ จาก map ของสินค้า
    for r in order_rows:
        for it in r["items"]:
            if not it["category"]:
                it["category"] = sku_cat.get(it["sku"], "")

    dates = sorted({r["date"] for r in order_rows if r["date"]})
    cats = sorted({i["category"] for r in order_rows for i in r["items"] if i["category"]})
    regions = sorted({r["region"] for r in order_rows if r["region"]})

    result = {
        "meta": {
            "currency": "THB",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "days": len(dates),
            "lookback_days": LOOKBACK_DAYS,
            "platforms": ["tiktok", "shopee", "lazada"],
            "categories": cats,
            "regions": regions,
        },
        "orders": order_rows,
        "products": list(pmap.values()),
        "ads": [],
    }
    if debug:
        result["_debug"] = {
            "lookback_days": LOOKBACK_DAYS,
            "order_count": len(order_rows),
            "category_tree_size": len(idname),
            "category_sample": dict(list(idname.items())[:15]),
            "product_sample": raw_product_sample,
            "finance": _finance_debug(app_key, app_secret, access, start, end),
        }
    return result


def db_stats():
    """เช็คช่วงข้อมูลใน DB แบบเบา ๆ (ไม่ดึงทุกแถว) — ใช้กับ /api/data?stats=1"""
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

    def _count(table):
        r = requests.get(f"{SB_URL}/rest/v1/{table}?select=*&limit=1",
                         headers={**h, "Prefer": "count=exact", "Range": "0-0"}, timeout=20)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0

    def _one(query):
        r = requests.get(f"{SB_URL}/rest/v1/lz_orders?{query}", headers=h, timeout=20)
        rows = r.json()
        return rows[0].get("date") if rows else None

    first = _one("select=date&order=date.asc&limit=1")
    last = _one("select=date&order=date.desc&limit=1")
    span = None
    if first and last:
        span = (datetime.date.fromisoformat(last) - datetime.date.fromisoformat(first)).days + 1

    # นับออเดอร์ราย "เดือน" และจำนวน "วันที่มีออเดอร์" ในแต่ละเดือน
    by_month, days_set = {}, {}
    for r in _sb_all("lz_orders", "date"):
        d = (r.get("date") or "")[:10]
        if len(d) < 7:
            continue
        mo = d[:7]
        by_month[mo] = by_month.get(mo, 0) + 1
        days_set.setdefault(mo, set()).add(d)

    months = []
    if first and last:
        cur = datetime.date.fromisoformat(first[:7] + "-01")
        end = datetime.date.fromisoformat(last[:7] + "-01")
        while cur <= end:
            mo = cur.strftime("%Y-%m")
            months.append({"month": mo, "orders": by_month.get(mo, 0),
                           "active_days": len(days_set.get(mo, set()))})
            cur = (cur.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)

    empty_months = [m["month"] for m in months if m["orders"] == 0]
    return {
        "orders": _count("lz_orders"),
        "order_items": _count("lz_order_items"),
        "products": _count("lz_products"),
        "first_order_date": first,
        "last_order_date": last,
        "span_days": span,
        "empty_months": empty_months,
        "by_month": months,
    }


def summary_week():
    """สรุปรายสัปดาห์แบบเบา ๆ สำหรับส่งอัตโนมัติ — /api/data?summary=week"""
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    today = datetime.date.today()
    d7 = today - datetime.timedelta(days=7)
    d14 = today - datetime.timedelta(days=14)
    orders = _sb_all("lz_orders", "order_id,date,status", "&date=gte." + d14.isoformat())
    bad = ("canceled", "cancelled", "returned", "failed", "refund", "refunded")
    valid = [o for o in orders if str(o.get("status", "")).lower() not in bad]
    omap = {o["order_id"]: o for o in valid}
    oids = list(omap.keys())
    items = []
    for i in range(0, len(oids), 80):
        ids = ",".join('"%s"' % x for x in oids[i:i + 80])
        r = requests.get(f"{SB_URL}/rest/v1/lz_order_items?select=order_id,name,sku,qty,price&order_id=in.({ids})",
                         headers=h, timeout=25)
        if r.ok:
            items += r.json()
    cut = d7.isoformat()
    rev = {"cur": 0.0, "prev": 0.0}
    ordset = {"cur": set(), "prev": set()}
    prod = {}
    for it in items:
        o = omap.get(it["order_id"])
        if not o:
            continue
        w = "cur" if str(o.get("date", "")) >= cut else "prev"
        v = float(it.get("price", 0) or 0) * int(it.get("qty", 1) or 1)
        rev[w] += v
        ordset[w].add(it["order_id"])
        if w == "cur":
            k = it.get("name") or it.get("sku") or "-"
            prod[k] = prod.get(k, 0) + v
    top = sorted(prod.items(), key=lambda x: -x[1])[:5]
    prods = _sb_all("lz_products", "sku,name,stock_lazada")
    out = [p for p in prods if int(p.get("stock_lazada", 0) or 0) == 0]
    low = [p for p in prods if 0 < int(p.get("stock_lazada", 0) or 0) < 30]
    return {
        "period": {"this_week_from": cut, "to": today.isoformat()},
        "revenue_this_week": round(rev["cur"], 2),
        "revenue_last_week": round(rev["prev"], 2),
        "growth_pct": round((rev["cur"] - rev["prev"]) / rev["prev"] * 100, 1) if rev["prev"] else None,
        "orders_this_week": len(ordset["cur"]),
        "orders_last_week": len(ordset["prev"]),
        "top_products": [{"name": n, "revenue": round(v, 2)} for n, v in top],
        "out_of_stock": len(out),
        "low_stock": len(low),
        "low_stock_samples": [p.get("name") for p in low[:5]],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pw = qs.get("pw", [""])[0]
        debug = qs.get("debug", ["0"])[0] in ("1", "true", "yes")
        stats = qs.get("stats", ["0"])[0] in ("1", "true", "yes")
        summary = qs.get("summary", [""])[0]
        fincheck = qs.get("fincheck", ["0"])[0] in ("1", "true", "yes")
        ordersample = qs.get("ordersample", ["0"])[0] in ("1", "true", "yes")
        apiprobe = qs.get("apiprobe", ["0"])[0] in ("1", "true", "yes")
        reviewsample = qs.get("reviewsample", ["0"])[0] in ("1", "true", "yes")
        if pw != os.environ.get("DASH_PASSWORD", ""):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            if fincheck:
                self._send(200, finance_check())
                return
            if ordersample:
                self._send(200, order_sample())
                return
            if apiprobe:
                self._send(200, api_probe())
                return
            if reviewsample:
                self._send(200, review_sample())
                return
            if summary == "week" and SB_URL and SB_KEY:
                self._send(200, summary_week())
                return
            if stats and SB_URL and SB_KEY:
                self._send(200, db_stats())
                return
            if SB_URL and SB_KEY and not debug:
                data = build_from_db()          # มี Supabase -> อ่านจาก DB (เร็ว)
            else:
                data = build_data(debug=debug)  # ยังไม่ตั้ง DB -> ดึง Lazada สดแบบเดิม
            self._send(200, data, cache=not debug)
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj, cache=False):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache:
            # fresh 10 นาที · หลังจากนั้นเสิร์ฟของเดิมทันทีแล้วอัปเดตเบื้องหลังได้ถึง 1 วัน
            self.send_header("Cache-Control", "s-maxage=600, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(body)
