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
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "120"))


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


def _refresh(app_key, app_secret, refresh_token):
    d = _call(AUTH_BASE, "/auth/token/refresh", app_key, app_secret, None,
              {"refresh_token": refresh_token})
    if not d.get("access_token"):
        raise RuntimeError(f"refresh failed: {d}")
    return d["access_token"]


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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pw = qs.get("pw", [""])[0]
        debug = qs.get("debug", ["0"])[0] in ("1", "true", "yes")
        if pw != os.environ.get("DASH_PASSWORD", ""):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            data = build_data(debug=debug)
            self._send(200, data, cache=not debug)
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj, cache=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache:
            self.send_header("Cache-Control", "s-maxage=900, stale-while-revalidate=600")
        self.end_headers()
        self.wfile.write(body)
