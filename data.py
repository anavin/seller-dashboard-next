"""
Vercel Python serverless function: /api/data
ดึงข้อมูล Lazada สด -> คืน JSON ให้หน้า dashboard (เช็ครหัสผ่านก่อน)

ตั้ง Environment Variables ใน Vercel (Project → Settings → Environment Variables):
    DASH_PASSWORD, LZ_APP_KEY, LZ_APP_SECRET, LZ_REFRESH_TOKEN
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import time
import hmac
import hashlib
import datetime
import urllib.parse
from collections import defaultdict

import requests

LAZADA_BASE = "https://api.lazada.co.th/rest"
AUTH_BASE = "https://auth.lazada.com/rest"


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


def build_data():
    app_key = os.environ["LZ_APP_KEY"]
    app_secret = os.environ["LZ_APP_SECRET"]
    refresh_token = os.environ["LZ_REFRESH_TOKEN"]
    access = _refresh(app_key, app_secret, refresh_token)

    now = datetime.datetime.now().astimezone()
    start = (now - datetime.timedelta(days=60)).replace(microsecond=0).isoformat()
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
        if offset >= 2000:
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
            "category": "",  # Lazada ไม่ส่งหมวดหมู่จริง (variation = ขนาด/รุ่น ไม่ใช่หมวด)
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

    # ---- products ----
    pmap = {}
    offset = 0
    while True:
        d = _call(LAZADA_BASE, "/products/get", app_key, app_secret, access,
                  {"limit": 50, "offset": offset, "filter": "all"})
        batch = d.get("data", {}).get("products", [])
        if not batch:
            break
        for p in batch:
            name = (p.get("attributes") or {}).get("name", "")
            for sk in p.get("skus", [{}]):
                sku = sk.get("SellerSku") or sk.get("ShopSku", "")
                rec = pmap.setdefault(sku, {"sku": sku, "name": name, "category": "",
                                            "price": float(sk.get("price", 0) or 0),
                                            "cost": 0.0,
                                            "stock": {"tiktok": 0, "shopee": 0, "lazada": 0}})
                rec["stock"]["lazada"] = int(sk.get("quantity", 0) or 0)
        if len(batch) < 50:
            break
        offset += 50
        if offset >= 2000:
            break

    dates = sorted({r["date"] for r in order_rows if r["date"]})
    cats = sorted({i["category"] for r in order_rows for i in r["items"] if i["category"]})
    regions = sorted({r["region"] for r in order_rows if r["region"]})

    return {
        "meta": {
            "currency": "THB",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "days": len(dates),
            "platforms": ["tiktok", "shopee", "lazada"],
            "categories": cats,
            "regions": regions,
        },
        "orders": order_rows,
        "products": list(pmap.values()),
        "ads": [],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        pw = urllib.parse.parse_qs(qs).get("pw", [""])[0]
        if pw != os.environ.get("DASH_PASSWORD", ""):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            data = build_data()
            self._send(200, data, cache=True)
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
