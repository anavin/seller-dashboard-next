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


# ---------- sync ----------
def run_sync():
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    now = datetime.datetime.now().astimezone()
    # อ่านสถานะล่าสุด + refresh_token ที่เก็บไว้ (กัน token หมดอายุ — ต่ออายุเองทุกครั้ง)
    state = sb_get("lz_sync", "id=eq.1&select=last_sync_time,refresh_token")
    stored_rt = state[0].get("refresh_token") if state else None
    rt = stored_rt or os.environ["LZ_REFRESH_TOKEN"]
    access, new_rt = _refresh(app_key, app_secret, rt)
    last = state[0]["last_sync_time"] if state and state[0].get("last_sync_time") else None
    if last:
        since = datetime.datetime.fromisoformat(last.replace("Z", "+00:00")) - datetime.timedelta(minutes=15)
    else:
        days = int(os.environ.get("INITIAL_SYNC_DAYS", "90"))
        since = now - datetime.timedelta(days=days)
    since_iso = since.replace(microsecond=0).isoformat()

    # ดึงเฉพาะออเดอร์ที่ "อัปเดตหลัง" since (ทั้งใหม่และเปลี่ยนสถานะ)
    raw, offset = [], 0
    while True:
        d = _lz("/orders/get", app_key, app_secret, access,
                {"update_after": since_iso, "limit": 100, "offset": offset, "sort_direction": "DESC"})
        batch = d.get("data", {}).get("orders", [])
        raw += batch
        if len(batch) < 100:
            break
        offset += 100
        if offset >= 5000:
            break

    # items แบบ batch
    items_by = {}
    ids = [str(o.get("order_id")) for o in raw]
    for i in range(0, len(ids), 50):
        d = _lz("/orders/items/get", app_key, app_secret, access,
                {"order_ids": json.dumps(ids[i:i + 50])})
        for od in d.get("data", []):
            items_by[str(od.get("order_id"))] = od.get("order_items", [])

    # products (เต็ม) + แผนที่หมวดหมู่
    idn = _category_map(app_key, app_secret, access)
    sku_cat, prod_rows = {}, {}
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

    # เตรียม rows สำหรับ DB
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
            "created_at_lz": str(o.get("created_at", "")),
        })
        for n, it in enumerate(items_by.get(oid, [])):
            sku = it.get("sku") or it.get("shop_sku", "")
            item_rows.append({
                "order_id": oid, "line": n, "sku": sku,
                "name": it.get("name", ""), "category": sku_cat.get(sku, ""),
                "qty": 1, "price": float(it.get("paid_price") or it.get("item_price") or 0), "cost": 0.0,
            })

    # เขียนลง DB
    if order_rows:
        sb_upsert("lz_orders", order_rows, "order_id")
        sb_delete_items([r["order_id"] for r in order_rows])
        sb_upsert("lz_order_items", item_rows, "order_id,line")
    if prod_rows:
        sb_upsert("lz_products", list(prod_rows.values()), "sku")

    sb_upsert("lz_sync", [{"id": 1, "last_sync_time": now.isoformat(),
                           "refresh_token": new_rt, "updated_at": now.isoformat()}], "id")

    return {"ok": True, "synced_orders": len(order_rows), "items": len(item_rows),
            "products": len(prod_rows), "since": since_iso}


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
        try:
            self._send(200, run_sync())
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
