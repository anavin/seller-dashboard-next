"""
Vercel function: /api/sync
ดึง "เฉพาะออเดอร์ใหม่/อัปเดต" จาก Lazada -> upsert ลง Supabase
รันอัตโนมัติด้วย Vercel Cron (ดู vercel.json) หรือกดเองที่ /api/sync?pw=<รหัส>

Environment Variables ที่ต้องมี:
    LZ_APP_KEY, LZ_APP_SECRET, LZ_REFRESH_TOKEN, DASH_PASSWORD
    SUPABASE_URL, SUPABASE_SERVICE_KEY
    (ออปชัน) INITIAL_SYNC_DAYS = ครั้งแรกดึงย้อนหลังกี่วัน (ค่าเริ่มต้น 365)
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import time
import hmac
import hashlib
import datetime
import calendar
import re
import urllib.parse

import requests

LAZADA_BASE = "https://api.lazada.co.th/rest"
AUTH_BASE = "https://auth.lazada.com/rest"
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ---------- Lazada ----------
def _sign(secret, path, params):
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), (path + ordered).encode(), hashlib.sha256).hexdigest().upper()


def _lz(path, app_key, app_secret, access, extra=None, base=LAZADA_BASE):
    params = {"app_key": app_key, "timestamp": str(int(time.time() * 1000)), "sign_method": "sha256"}
    if access:
        params["access_token"] = access
    if extra:
        params.update({k: str(v) for k, v in extra.items()})
    params["sign"] = _sign(app_secret, path, params)
    r = requests.get(base + path, params=params, timeout=25)
    r.raise_for_status()
    d = r.json()
    if str(d.get("code", "0")) not in ("0", ""):
        raise RuntimeError(f"Lazada error [{path}]: {d.get('code')} {d.get('message')}")
    return d


def _lz_soft(path, *a, **k):
    try:
        return _lz(path, *a, **k)
    except Exception:
        return None


def _refresh(app_key, app_secret, rt):
    d = _lz("/auth/token/refresh", app_key, app_secret, None, {"refresh_token": rt}, base=AUTH_BASE)
    if not d.get("access_token"):
        raise RuntimeError(f"refresh failed: {d}")
    return d["access_token"], d.get("refresh_token", rt)


def _hour(s):
    try:
        return int(str(s)[11:13])
    except Exception:
        return 12


def _category_map(app_key, app_secret, access):
    d = _lz_soft("/category/tree/get", app_key, app_secret, access)
    idn = {}
    if not d:
        return idn

    def walk(ns):
        for n in ns or []:
            if n.get("category_id") is not None and n.get("name"):
                idn[str(n["category_id"])] = n["name"]
            walk(n.get("children"))
    walk(d.get("data") or [])
    return idn


# ---------- Supabase (PostgREST) ----------
def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def sb_get(table, query):
    r = requests.get(f"{SB_URL}/rest/v1/{table}?{query}", headers=sb_headers(), timeout=25)
    r.raise_for_status()
    return r.json()


def sb_count(table, query):
    """นับจำนวนแถวแบบเบา ๆ (ไม่ดึงข้อมูล) ผ่าน content-range header"""
    h = sb_headers({"Prefer": "count=exact", "Range": "0-0"})
    r = requests.get(f"{SB_URL}/rest/v1/{table}?{query}", headers=h, timeout=20)
    cr = r.headers.get("content-range", "*/0")
    return int(cr.split("/")[-1]) if "/" in cr else 0


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          json=chunk, timeout=25)
        r.raise_for_status()


def sb_delete_items(order_ids):
    for i in range(0, len(order_ids), 100):
        chunk = order_ids[i:i + 100]
        ids = ",".join(f'"{x}"' for x in chunk)
        r = requests.delete(f"{SB_URL}/rest/v1/lz_order_items?order_id=in.({ids})",
                            headers=sb_headers({"Prefer": "return=minimal"}), timeout=25)
        r.raise_for_status()


# ---------- sync helpers ----------
def _fetch_orders(access, app_key, app_secret, q_filter):
    """ดึงออเดอร์ตามเงื่อนไข (created window หรือ update_after) แบบแบ่งหน้า"""
    raw, offset = [], 0
    while True:
        q = {"limit": 100, "offset": offset, "sort_direction": "DESC"}
        q.update(q_filter)
        d = _lz("/orders/get", app_key, app_secret, access, q)
        batch = d.get("data", {}).get("orders", [])
        raw += batch
        if len(batch) < 100:
            break
        offset += 100
        if offset >= 20000:
            break
    return raw


def _fetch_items(access, app_key, app_secret, ids):
    items_by = {}
    for i in range(0, len(ids), 50):
        d = _lz("/orders/items/get", app_key, app_secret, access,
                {"order_ids": json.dumps(ids[i:i + 50])})
        for od in d.get("data", []):
            items_by[str(od.get("order_id"))] = od.get("order_items", [])
    return items_by


def _buyer_key(o):
    """ลายนิ้วมือผู้ซื้อจากที่อยู่จัดส่ง (ชื่อ+เบอร์+ที่อยู่+รหัสไปรษณีย์) -> hash สั้น ๆ"""
    a = o.get("address_shipping") or {}
    parts = [a.get("first_name"), a.get("last_name"), a.get("phone"), a.get("phone2"),
             a.get("address1"), a.get("address2"), a.get("post_code"), a.get("city")]
    s = "|".join(str(p).strip().lower() for p in parts if p)
    if not s:
        return ""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _build_rows(raw, items_by, sku_cat):
    order_rows, item_rows = [], []
    for o in raw:
        oid = str(o.get("order_id"))
        statuses = o.get("statuses") or ["unknown"]
        order_rows.append({
            "order_id": oid,
            "date": str(o.get("created_at", ""))[:10] or None,
            "hour": _hour(o.get("created_at", "")),
            "platform": "lazada",
            "status": str(statuses[0]).lower(),
            "region": (o.get("address_shipping") or {}).get("city", "") or "",
            "customer": "new",
            "shipping_fee": float(o.get("shipping_fee", 0) or 0),
            "platform_fee": 0.0,
            "buyer_key": _buyer_key(o),
            "payment_method": str(o.get("payment_method", "") or "").strip(),
            "created_at_lz": str(o.get("created_at", "")),
        })
        for n, it in enumerate(items_by.get(oid, [])):
            sku = it.get("sku") or it.get("shop_sku", "")
            item_rows.append({
                "order_id": oid, "line": n, "sku": sku,
                "name": it.get("name", ""), "category": sku_cat.get(sku, ""),
                "qty": 1, "price": float(it.get("paid_price") or it.get("item_price") or 0), "cost": 0.0,
            })
    return order_rows, item_rows


def _write_orders(order_rows, item_rows):
    if order_rows:
        sb_upsert("lz_orders", order_rows, "order_id")
        sb_delete_items([r["order_id"] for r in order_rows])
        sb_upsert("lz_order_items", item_rows, "order_id,line")


def _month_windows(start, end):
    """แตกช่วง [start, end] เป็นก้อนรายเดือน (clamp ที่ขอบ) เพื่อ commit ทีละเดือน"""
    out, cur = [], start.replace(day=1)
    while cur <= end:
        last = calendar.monthrange(cur.year, cur.month)[1]
        out.append((max(start, cur), min(end, cur.replace(day=last))))
        cur = cur.replace(day=last) + datetime.timedelta(days=1)
    return out


def _num(v):
    """ดึงตัวเลขจาก string เช่น '1100.44 THB' หรือ '-245.07' -> float"""
    if v is None:
        return 0.0
    m = re.search(r"-?\d[\d,]*(\.\d+)?", str(v))
    return float(m.group().replace(",", "")) if m else 0.0


def _is_timeout(msg):
    return ("ServiceTimeout" in msg) or ("timeout" in msg.lower()) or ("RPC" in msg)


def _txn_pull(access, app_key, app_secret, start_time, end_time):
    """ดึง transaction details ช่วง [start, end] (อาจ throw ServiceTimeout ถ้าช่วงกว้าง)"""
    rows, offset = [], 0
    while True:
        d = _lz("/finance/transaction/details/get", app_key, app_secret, access,
                {"start_time": start_time, "end_time": end_time, "limit": 500, "offset": offset})
        batch = d.get("data") or []
        if isinstance(batch, dict):
            batch = batch.get("list") or batch.get("rows") or []
        if not batch:
            break
        rows += batch
        if len(batch) < 500:
            break
        offset += 500
        if offset >= 200000:
            break
    return rows


def _txn_sums(access, app_key, app_secret, after, before, depth=0):
    """รวมยอด transaction details ช่วง [after, before] -> {rev, fee, n} · timeout = ลองซ้ำ/หดช่วง"""
    last_err = None
    for attempt in range(2):
        try:
            rows = _txn_pull(access, app_key, app_secret, after, before)
            rev = fee = 0.0
            for r in rows:
                amt = _num(r.get("amount"))
                if amt >= 0:
                    rev += amt
                else:
                    fee += amt
            return {"rev": rev, "fee": fee, "n": len(rows)}
        except Exception as e:
            last_err = e
            if not _is_timeout(str(e)):
                raise
            time.sleep(1.0)
    a = datetime.date.fromisoformat(after)
    b = datetime.date.fromisoformat(before)
    if depth < 7 and (b - a).days >= 1:
        mid = a + datetime.timedelta(days=(b - a).days // 2)
        l = _txn_sums(access, app_key, app_secret, after, mid.isoformat(), depth + 1)
        r = _txn_sums(access, app_key, app_secret, (mid + datetime.timedelta(days=1)).isoformat(), before, depth + 1)
        return {"rev": l["rev"] + r["rev"], "fee": l["fee"] + r["fee"], "n": l["n"] + r["n"]}
    raise last_err


def finance_upsert_month(access, app_key, app_secret, y, m):
    """ดึงค่าธรรมเนียมทั้งเดือน (y, m) แล้ว upsert เป็น 1 แถวต่อเดือนใน lz_finance"""
    last = calendar.monthrange(y, m)[1]
    ws = datetime.date(y, m, 1)
    we = datetime.date(y, m, last)
    s = _txn_sums(access, app_key, app_secret, ws.isoformat(), we.isoformat())
    sb_upsert("lz_finance", [{
        "statement_number": "M-%04d-%02d" % (y, m),
        "date": ws.isoformat(),
        "item_revenue": round(s["rev"], 2),
        "fees_total": round(s["fee"], 2),
        "refunds": 0,
        "payout": round(s["rev"] + s["fee"], 2),
        "created_at_lz": "",
    }], "statement_number")
    return s["n"]


def _rev_get(r, keys, default=None):
    for k in keys:
        v = r.get(k)
        if v not in (None, "", []):
            return v
    return default


def run_reviews_sync():
    """ดึงรีวิวสินค้าทั้งหมด: วน item_id -> /review/seller/list/v2 -> upsert lz_reviews"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    now = datetime.datetime.now().astimezone()
    state = sb_get("lz_sync", "id=eq.1&select=refresh_token")
    rt = (state[0].get("refresh_token") if state else None) or os.environ["LZ_REFRESH_TOKEN"]
    access, new_rt = _refresh(app_key, app_secret, rt)
    # ดึง item_id ทั้งหมด + map ชื่อ/sku
    meta, item_ids, offset = {}, [], 0
    while True:
        d = _lz("/products/get", app_key, app_secret, access, {"limit": 50, "offset": offset, "filter": "all"})
        batch = d.get("data", {}).get("products", [])
        if not batch:
            break
        for p in batch:
            iid = p.get("item_id")
            if not iid:
                continue
            nm = (p.get("attributes") or {}).get("name", "")
            sk = ""
            for s in (p.get("skus") or [{}]):
                sk = s.get("SellerSku") or s.get("ShopSku") or ""
                break
            meta[str(iid)] = (sk, nm)
            item_ids.append(iid)
        if len(batch) < 50:
            break
        offset += 50
        if offset >= 10000:
            break
    # ดึงรีวิว "ล่าสุด" ต่อสินค้า ผ่าน history (Lazada รับแค่ ms + ช่วงสั้น ~7 วัน)
    ms_b = int(now.timestamp() * 1000)
    ms_a = int((now - datetime.timedelta(days=7)).timestamp() * 1000)
    rows = {}
    t0 = time.time()
    processed, more = 0, False
    for iid in item_ids:
        if time.time() - t0 > 50:   # กัน Vercel timeout — เกินงบเวลาแล้วหยุด
            more = True
            break
        processed += 1
        sk, nm = meta.get(str(iid), ("", ""))
        current = 1
        while True:
            try:
                d = _lz("/review/seller/history/list", app_key, app_secret, access,
                        {"item_id": iid, "start_time": ms_a, "end_time": ms_b, "current": current, "page_size": 50})
            except Exception:
                break
            data = d.get("data")
            rlist = []
            if isinstance(data, dict):
                rlist = data.get("review_list") or data.get("reviews") or data.get("list") or []
            elif isinstance(data, list):
                rlist = data
            if not rlist:
                break
            for r in rlist:
                rid = str(_rev_get(r, ["review_id", "id", "reviewId"], "") or "")
                if not rid:
                    continue
                rows[rid] = {
                    "review_id": rid,
                    "item_id": str(iid),
                    "sku": sk,
                    "name": (_rev_get(r, ["item_name", "product_name", "title"], "") or nm or sk),
                    "rating": int(_rev_get(r, ["rating", "review_rating", "star", "score"], 0) or 0),
                    "content": str(_rev_get(r, ["review_content", "buyer_review", "content", "comment", "reviewContent"], "") or ""),
                    "review_time": str(_rev_get(r, ["review_time", "gmt_create", "create_time", "reviewTime", "createTime"], "") or ""),
                    "has_reply": bool(_rev_get(r, ["seller_reply", "reply", "sellerReply"], "")),
                }
            if len(rlist) < 50:
                break
            current += 1
            if current > 20:
                break
    if rows:
        sb_upsert("lz_reviews", list(rows.values()), "review_id")
    sb_upsert("lz_sync", [{"id": 1, "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")
    return {"ok": True, "mode": "reviews", "items_processed": processed,
            "total_items": len(item_ids), "reviews": len(rows), "more": more}


def run_finance_sync(since="2023-01-01"):
    """ดึงข้อมูลการเงินทีละเดือน (กัน Lazada RPC timeout) ข้ามเดือนที่มีแล้ว — เรียกซ้ำจนกว่า more=false"""
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    now = datetime.datetime.now().astimezone()
    state = sb_get("lz_sync", "id=eq.1&select=refresh_token")
    rt = (state[0].get("refresh_token") if state else None) or os.environ["LZ_REFRESH_TOKEN"]
    access, new_rt = _refresh(app_key, app_secret, rt)
    start = datetime.date.fromisoformat(since)
    end = now.date()
    budget, filled, skipped, more, total = 3, [], 0, False, 0
    for ws, we in _month_windows(start, end):
        if budget <= 0:
            more = True
            break
        mo = ws.strftime("%Y-%m")
        try:
            cnt = sb_count("lz_finance", "select=statement_number&statement_number=eq.M-%s" % mo)
        except Exception:
            cnt = 0
        if cnt > 0:
            skipped += 1
            continue
        n = finance_upsert_month(access, app_key, app_secret, ws.year, ws.month)
        filled.append({"month": mo, "lines": n})
        total += n
        budget -= 1
    sb_upsert("lz_sync", [{"id": 1, "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")
    return {"ok": True, "mode": "finance", "txn_lines_added": total, "filled": filled,
            "skipped_existing": skipped, "more": more,
            "hint": "more=true → เรียก URL เดิมซ้ำอีกครั้งจนกว่าจะ false"}


# ---------- sync ----------
def run_sync(force_days=None, from_date=None, to_date=None, fill_all=False, since_date=None):
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    now = datetime.datetime.now().astimezone()
    backfill = bool(force_days or from_date or to_date or fill_all)
    # อ่านสถานะล่าสุด + refresh_token ที่เก็บไว้ (กัน token หมดอายุ — ต่ออายุเองทุกครั้ง)
    state = sb_get("lz_sync", "id=eq.1&select=last_sync_time,refresh_token")
    stored_rt = state[0].get("refresh_token") if state else None
    rt = stored_rt or os.environ["LZ_REFRESH_TOKEN"]
    access, new_rt = _refresh(app_key, app_secret, rt)
    last = state[0]["last_sync_time"] if state and state[0].get("last_sync_time") else None

    def _isod(d, end=False):
        t = datetime.time(23, 59, 59) if end else datetime.time(0, 0, 0)
        return datetime.datetime.combine(d, t).replace(tzinfo=now.tzinfo).isoformat()

    if fill_all:
        # โหมดเติมอัตโนมัติ: ไล่เช็คทีละเดือน เดือนไหนยังไม่มีข้อมูลใน DB ค่อยดึง
        # ดึงได้สูงสุด 4 เดือน/รอบ (กัน timeout) — ถ้ายังไม่ครบจะตอบ more=true ให้เรียกซ้ำ
        start = datetime.date.fromisoformat(since_date) if since_date else datetime.date(2024, 1, 1)
        end = now.date()
        sku_cat = {}
        try:
            for r in sb_get("lz_products", "select=sku,category&limit=10000"):
                if r.get("sku"):
                    sku_cat[r["sku"]] = r.get("category") or ""
        except Exception:
            pass
        filled, skipped, budget, more = [], 0, 4, False
        for ws, we in _month_windows(start, end):
            if budget <= 0:
                more = True
                break
            try:
                cnt = sb_count("lz_orders", "select=order_id&date=gte.%s&date=lte.%s"
                               % (ws.isoformat(), we.isoformat()))
            except Exception:
                cnt = 0
            if cnt > 0:
                skipped += 1
                continue
            raw = _fetch_orders(access, app_key, app_secret,
                                {"created_after": _isod(ws), "created_before": _isod(we, end=True)})
            items_by = _fetch_items(access, app_key, app_secret, [str(o.get("order_id")) for o in raw])
            orows, irows = _build_rows(raw, items_by, sku_cat)
            _write_orders(orows, irows)
            filled.append({"month": ws.strftime("%Y-%m"), "orders": len(orows)})
            budget -= 1
        sb_upsert("lz_sync", [{"id": 1, "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")
        return {"ok": True, "mode": "fill_all", "filled": filled, "skipped_existing": skipped,
                "more": more, "range": "%s..%s" % (start.isoformat(), end.isoformat()),
                "hint": "more=true → เรียก URL เดิมซ้ำอีกครั้งจนกว่าจะ false"}

    if backfill:
        # ดึงตาม "วันที่สร้างออเดอร์" ทีละเดือน + commit ทุกเดือน (timeout ก็ไม่เสียของเก่า)
        if from_date:
            start = datetime.date.fromisoformat(from_date)
        else:
            start = (now - datetime.timedelta(days=int(force_days))).date()
        end = datetime.date.fromisoformat(to_date) if to_date else now.date()
        # หมวดหมู่ของ SKU จากที่มีใน DB (ไม่ดึง products ใหม่ กัน timeout)
        sku_cat = {}
        try:
            for r in sb_get("lz_products", "select=sku,category&limit=10000"):
                if r.get("sku"):
                    sku_cat[r["sku"]] = r.get("category") or ""
        except Exception:
            pass
        months, total_o, total_i = [], 0, 0
        for ws, we in _month_windows(start, end):
            raw = _fetch_orders(access, app_key, app_secret,
                                {"created_after": _isod(ws), "created_before": _isod(we, end=True)})
            items_by = _fetch_items(access, app_key, app_secret, [str(o.get("order_id")) for o in raw])
            orows, irows = _build_rows(raw, items_by, sku_cat)
            _write_orders(orows, irows)  # commit ทันทีต่อเดือน
            total_o += len(orows)
            total_i += len(irows)
            months.append({"month": ws.strftime("%Y-%m"), "orders": len(orows)})
        # backfill ไม่ขยับ last_sync_time — อัปเดตแค่ token
        sb_upsert("lz_sync", [{"id": 1, "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")
        return {"ok": True, "synced_orders": total_o, "items": total_i, "backfill": True,
                "window": "%s..%s" % (start.isoformat(), end.isoformat()), "months": months}

    # ---------- sync ปกติ (incremental) ----------
    if last:
        since = datetime.datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.timedelta(minutes=15)
    else:
        since = now - datetime.timedelta(days=int(os.environ.get("INITIAL_SYNC_DAYS", "90")))
    since_iso = since.replace(microsecond=0).isoformat()

    raw = _fetch_orders(access, app_key, app_secret, {"update_after": since_iso})
    items_by = _fetch_items(access, app_key, app_secret, [str(o.get("order_id")) for o in raw])

    # ดึง products (เต็ม) + แผนที่หมวดหมู่
    sku_cat, prod_rows = {}, {}
    idn = _category_map(app_key, app_secret, access)
    offset = 0
    while True:
        d = _lz("/products/get", app_key, app_secret, access, {"limit": 50, "offset": offset, "filter": "all"})
        batch = d.get("data", {}).get("products", [])
        if not batch:
            break
        for p in batch:
            nm = (p.get("attributes") or {}).get("name", "")
            pcat = idn.get(str(p.get("primary_category") or ""), "")
            for sk in p.get("skus", [{}]):
                sku = sk.get("SellerSku") or sk.get("ShopSku", "")
                if not sku:
                    continue
                prod_rows[sku] = {"sku": sku, "name": nm, "category": pcat,
                                  "price": float(sk.get("price", 0) or 0), "cost": 0.0,
                                  "stock_lazada": int(sk.get("quantity", 0) or 0)}
                if pcat:
                    sku_cat[sku] = pcat
        if len(batch) < 50:
            break
        offset += 50
        if offset >= 5000:
            break

    order_rows, item_rows = _build_rows(raw, items_by, sku_cat)
    _write_orders(order_rows, item_rows)
    if prod_rows:
        sb_upsert("lz_products", list(prod_rows.values()), "sku")
    # อัปเดตการเงิน (เดือนนี้ + เดือนก่อน) ให้สดทุกวัน
    fin = 0
    try:
        cur = now.date()
        prev = (cur.replace(day=1) - datetime.timedelta(days=1))
        fin += finance_upsert_month(access, app_key, app_secret, cur.year, cur.month)
        fin += finance_upsert_month(access, app_key, app_secret, prev.year, prev.month)
    except Exception:
        pass
    sb_upsert("lz_sync", [{"id": 1, "last_sync_time": now.isoformat(),
                           "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")
    return {"ok": True, "synced_orders": len(order_rows), "items": len(item_rows),
            "products": len(prod_rows), "finance_statements": fin,
            "window": "update_after %s" % since_iso, "backfill": False}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pw = qs.get("pw", [""])[0]
        auth = self.headers.get("authorization", "")
        cron_ok = bool(os.environ.get("CRON_SECRET")) and auth == "Bearer " + os.environ.get("CRON_SECRET", "")
        if pw != os.environ.get("DASH_PASSWORD", "") and not cron_ok:
            self._send(401, {"error": "unauthorized"})
            return
        if not SB_URL or not SB_KEY:
            self._send(500, {"error": "ยังไม่ได้ตั้ง SUPABASE_URL / SUPABASE_SERVICE_KEY"})
            return
        force_days = qs.get("days", [None])[0]
        from_date = qs.get("from", [None])[0]
        to_date = qs.get("to", [None])[0]
        fill_all = qs.get("backfill", [""])[0] == "all"
        since_date = qs.get("since", [None])[0]
        finance = qs.get("finance", [None])[0]
        reviews = qs.get("reviews", ["0"])[0] in ("1", "true", "yes")
        try:
            if reviews:
                self._send(200, run_reviews_sync())
                return
            if finance:
                since = finance if finance not in ("1", "all", "true", "yes") else "2023-01-01"
                self._send(200, run_finance_sync(since))
                return
            self._send(200, run_sync(force_days=force_days, from_date=from_date, to_date=to_date,
                                     fill_all=fill_all, since_date=since_date))
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
