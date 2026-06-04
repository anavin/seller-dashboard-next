"""
Vercel function: /api/shopee_import
รับข้อมูลออเดอร์ Shopee ที่หน้าเว็บ parse จากไฟล์ export (.xlsx) แล้ว upsert ลง Supabase (sp_*)

ใช้คู่กับปุ่ม "นำเข้าไฟล์ Shopee" ในแดชบอร์ด:
  หน้าเว็บอ่านไฟล์ Order export ด้วย SheetJS -> ส่ง JSON มาที่ endpoint นี้ (POST)
  body = {"orders":[...], "items":[...]}
ป้องกันด้วยรหัสผ่าน (?pw=<DASH_PASSWORD>)

Environment: DASH_PASSWORD, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import datetime
import urllib.parse

import requests

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          json=chunk, timeout=30)
        r.raise_for_status()


def sb_delete_items(order_ids):
    for i in range(0, len(order_ids), 100):
        ids = ",".join(f'"{x}"' for x in order_ids[i:i + 100])
        r = requests.delete(f"{SB_URL}/rest/v1/sp_order_items?order_id=in.({ids})",
                            headers=sb_headers({"Prefer": "return=minimal"}), timeout=30)
        r.raise_for_status()


def _f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def _i(x, d=0):
    try:
        return int(float(x))
    except Exception:
        return d


def do_import(payload):
    raw_orders = payload.get("orders") or []
    raw_items = payload.get("items") or []

    orders = []
    for o in raw_orders:
        oid = str(o.get("order_id", "")).strip()
        if not oid:
            continue
        orders.append({
            "order_id": oid,
            "date": (str(o.get("date", "")) or "")[:10] or None,
            "hour": _i(o.get("hour", 12), 12),
            "platform": "shopee",
            "status": o.get("status", "") or "",
            "region": o.get("region", "") or "",
            "customer": "new",
            "shipping_fee": _f(o.get("shipping_fee", 0)),
            "platform_fee": _f(o.get("platform_fee", 0)),
            "created_at_sp": str(o.get("date", "") or ""),
            "buyer_key": o.get("buyer", "") or "",
            "payment_method": o.get("payment", "") or "",
        })

    items = []
    for it in raw_items:
        oid = str(it.get("order_id", "")).strip()
        if not oid:
            continue
        items.append({
            "order_id": oid,
            "line": _i(it.get("line", 0)),
            "sku": str(it.get("sku", "") or ""),
            "name": str(it.get("name", "") or ""),
            "category": str(it.get("category", "") or ""),
            "qty": _i(it.get("qty", 1), 1),
            "price": _f(it.get("price", 0)),
            "cost": _f(it.get("cost", 0)),
        })

    order_ids = [o["order_id"] for o in orders]
    if order_ids:
        sb_delete_items(order_ids)        # ลบรายการเก่าของออเดอร์เหล่านี้ก่อน (กันซ้ำ)
    sb_upsert("sp_orders", orders, "order_id")
    sb_upsert("sp_order_items", items, "order_id,line")
    # อัปเดตเวลา sync ล่าสุดของ Shopee (ไว้โชว์ในแดชบอร์ด)
    try:
        sb_upsert("sp_sync", [{"id": 1, "last_sync_time": datetime.datetime.utcnow().isoformat(),
                               "updated_at": datetime.datetime.utcnow().isoformat()}], "id")
    except Exception:
        pass
    return {"ok": True, "orders": len(orders), "items": len(items)}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pw = qs.get("pw", [""])[0]
        if pw != os.environ.get("DASH_PASSWORD", ""):
            self._send(401, {"error": "unauthorized"})
            return
        if not SB_URL or not SB_KEY:
            self._send(500, {"error": "ยังไม่ได้ตั้ง SUPABASE_URL / SUPABASE_SERVICE_KEY"})
            return
        try:
            length = int(self.headers.get("content-length", 0) or 0)
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            self._send(200, do_import(payload))
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
