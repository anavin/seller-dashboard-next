"""
Vercel function: /api/shopee_sync
ดึงออเดอร์ + สินค้า จาก Shopee Open Platform API v2 -> upsert ลง Supabase (sp_* tables)
รันเองที่ /api/shopee_sync?pw=<รหัส>  หรือผ่าน Vercel Cron (Bearer CRON_SECRET)

พารามิเตอร์:
  ?days=N         ดึงย้อนหลัง N วัน (ครั้งแรก/บังคับ)
  ?products=1     ซิงค์เฉพาะรายการสินค้า (สต็อก/ราคา/หมวด)
  (ไม่ใส่)         ซิงค์เพิ่มเฉพาะออเดอร์ใหม่นับจาก last_sync_time

ต้องอนุญาตร้านก่อน (ดู /api/shopee_auth) เพื่อให้มี token ใน sp_sync
Environment: SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY, DASH_PASSWORD,
             SUPABASE_URL, SUPABASE_SERVICE_KEY, (ออปชัน) CRON_SECRET, INITIAL_SYNC_DAYS
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

SHOPEE_HOST = os.environ.get("SHOPEE_HOST", "https://partner.shopeemobile.com").rstrip("/")
PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID", "")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TH_OFFSET = 7 * 3600          # Shopee ส่ง unix UTC — แปลงเป็นเวลาไทย
WINDOW = 14 * 24 * 3600       # get_order_list ครอบได้สูงสุด ~15 วัน/ครั้ง ใช้ 14 กันพลาด
TIME_BUDGET = 50              # วินาที (Vercel maxDuration 60)


# ---------- ลายเซ็น ----------
def _sign(path, ts, access="", shop_id=""):
    base = f"{PARTNER_ID}{path}{ts}{access}{shop_id}".encode()
    return hmac.new(PARTNER_KEY.encode(), base, hashlib.sha256).hexdigest()


def _common(path, access, shop_id):
    ts = int(time.time())
    return {"partner_id": PARTNER_ID, "timestamp": ts,
            "sign": _sign(path, ts, access, shop_id),
            "access_token": access, "shop_id": shop_id}


def _get(path, access, shop_id, extra=None):
    params = _common(path, access, shop_id)
    if extra:
        params.update(extra)
    r = requests.get(f"{SHOPEE_HOST}{path}", params=params, timeout=25)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(f"Shopee error [{path}]: {d.get('error')} {d.get('message')}")
    return d


# ---------- Supabase ----------
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
        ids = ",".join(f'"{x}"' for x in order_ids[i:i + 100])
        r = requests.delete(f"{SB_URL}/rest/v1/sp_order_items?order_id=in.({ids})",
                            headers=sb_headers({"Prefer": "return=minimal"}), timeout=25)
        r.raise_for_status()


# ---------- token ----------
def _load_state():
    rows = sb_get("sp_sync", "id=eq.1&select=shop_id,access_token,refresh_token,token_expire,last_sync_time")
    if not rows:
        raise RuntimeError("ยังไม่ได้อนุญาตร้าน Shopee — เปิด /api/shopee_auth?pw=...&link=1 ก่อน")
    return rows[0]


def _refresh_token(shop_id, refresh):
    path = "/api/v2/auth/access_token/get"
    ts = int(time.time())
    sign = _sign(path, ts)  # public sign (ไม่มี access/shop)
    url = f"{SHOPEE_HOST}{path}?" + urllib.parse.urlencode(
        {"partner_id": PARTNER_ID, "timestamp": ts, "sign": sign})
    body = {"refresh_token": refresh, "shop_id": int(shop_id), "partner_id": int(PARTNER_ID)}
    r = requests.post(url, json=body, timeout=25)
    r.raise_for_status()
    d = r.json()
    if d.get("error") or not d.get("access_token"):
        raise RuntimeError(f"refresh token ล้มเหลว: {d}")
    access = d["access_token"]
    new_refresh = d.get("refresh_token", refresh)
    expire_in = int(d.get("expire_in", 14400) or 14400)
    expire_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expire_in)
    sb_upsert("sp_sync", [{"id": 1, "access_token": access, "refresh_token": new_refresh,
                           "token_expire": expire_at.isoformat(),
                           "updated_at": datetime.datetime.utcnow().isoformat()}], "id")
    return access


def _ensure_token(state):
    """คืน (access, shop_id) ที่ใช้งานได้ — refresh ถ้าใกล้หมดอายุ"""
    shop_id = state.get("shop_id")
    access = state.get("access_token")
    refresh = state.get("refresh_token")
    if not shop_id or not refresh:
        raise RuntimeError("ไม่มี shop_id/refresh_token — อนุญาตร้านใหม่ที่ /api/shopee_auth")
    exp = state.get("token_expire")
    need = True
    if access and exp:
        try:
            expdt = datetime.datetime.fromisoformat(exp.replace("Z", ""))
            need = (expdt - datetime.datetime.utcnow()).total_seconds() < 600
        except Exception:
            need = True
    if need:
        access = _refresh_token(shop_id, refresh)
    return access, str(shop_id)


# ---------- แปลงข้อมูล ----------
def _buyer_key(addr):
    parts = [str(addr.get(k, "")) for k in
             ("name", "phone", "full_address", "town", "district", "city", "state", "zipcode")]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _map_status(s):
    s = (s or "").upper()
    if s in ("CANCELLED", "IN_CANCEL"):
        return "cancelled"
    if s in ("TO_RETURN", "RETURN", "RETURNED"):
        return "returned"
    if s in ("COMPLETED", "SHIPPED", "TO_CONFIRM_RECEIVE", "TO_SHIP", "PROCESSED"):
        return "completed"
    return s.lower() or "completed"


def _category_map(access, shop_id):
    """item_id -> ชื่อหมวด (ใช้ category tree ของร้าน)"""
    try:
        d = _get("/api/v2/product/get_category", access, shop_id, {"language": "th"})
        out = {}
        for c in (d.get("response") or {}).get("category_list") or []:
            cid = c.get("category_id")
            name = c.get("display_category_name") or c.get("original_category_name") or ""
            if cid is not None:
                out[int(cid)] = name
        return out
    except Exception:
        return {}


def _all_item_ids(access, shop_id):
    ids, offset = [], 0
    while True:
        d = _get("/api/v2/product/get_item_list", access, shop_id,
                 {"offset": offset, "page_size": 100, "item_status": "NORMAL"})
        resp = d.get("response") or {}
        batch = resp.get("item") or []
        ids += [it.get("item_id") for it in batch if it.get("item_id") is not None]
        if not resp.get("has_next_page"):
            break
        offset = resp.get("next_offset", offset + len(batch))
        if offset > 20000:
            break
    return ids


def sync_products():
    """ดึงรายการสินค้า -> sp_products และคืน map item_id -> {sku,name,category}"""
    state = _load_state()
    access, shop_id = _ensure_token(state)
    cat_by_id = _category_map(access, shop_id)
    ids = _all_item_ids(access, shop_id)
    prod_rows, item_meta = [], {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = _get("/api/v2/product/get_item_base_info", access, shop_id,
                 {"item_id_list": ",".join(str(x) for x in chunk)})
        for it in (d.get("response") or {}).get("item_list") or []:
            iid = it.get("item_id")
            sku = it.get("item_sku") or str(iid)
            name = it.get("item_name", "")
            cat = cat_by_id.get(int(it.get("category_id", 0) or 0), "")
            price = 0
            pinfo = it.get("price_info") or []
            if pinfo:
                price = float(pinfo[0].get("current_price", 0) or 0)
            stock = 0
            sinfo = it.get("stock_info_v2") or {}
            try:
                summ = sinfo.get("summary_info") or {}
                stock = int(summ.get("total_available_stock", 0) or 0)
            except Exception:
                stock = 0
            item_meta[iid] = {"sku": sku, "name": name, "category": cat}
            prod_rows.append({"sku": sku, "name": name, "category": cat,
                              "price": price, "cost": 0, "stock_shopee": stock})
    sb_upsert("sp_products", prod_rows, "sku")
    return item_meta, {"products": len(prod_rows)}


def _order_sns(access, shop_id, t_from, t_to, deadline):
    """ดึง order_sn ในช่วง [t_from,t_to] แบ่งหน้าทีละ 100"""
    sns, cursor = [], ""
    while True:
        d = _get("/api/v2/order/get_order_list", access, shop_id,
                 {"time_range_field": "create_time", "time_from": t_from, "time_to": t_to,
                  "page_size": 100, "cursor": cursor,
                  "response_optional_fields": "order_status"})
        resp = d.get("response") or {}
        sns += [o.get("order_sn") for o in resp.get("order_list") or [] if o.get("order_sn")]
        if not resp.get("more"):
            break
        cursor = resp.get("next_cursor", "")
        if not cursor or time.time() > deadline:
            break
    return sns


def _build_rows(access, shop_id, sns, item_meta):
    OPT = ("order_status,create_time,total_amount,actual_shipping_fee,"
           "payment_method,recipient_address,item_list,buyer_username")
    orders, items = [], []
    for i in range(0, len(sns), 50):
        chunk = sns[i:i + 50]
        d = _get("/api/v2/order/get_order_detail", access, shop_id,
                 {"order_sn_list": ",".join(chunk), "response_optional_fields": OPT})
        for o in (d.get("response") or {}).get("order_list") or []:
            osn = o.get("order_sn")
            if not osn:
                continue
            ct = int(o.get("create_time", 0) or 0)
            local = datetime.datetime.utcfromtimestamp(ct + TH_OFFSET) if ct else None
            addr = o.get("recipient_address") or {}
            orders.append({
                "order_id": osn,
                "date": local.strftime("%Y-%m-%d") if local else None,
                "hour": local.hour if local else 12,
                "platform": "shopee",
                "status": _map_status(o.get("order_status")),
                "region": addr.get("state", "") or "",
                "customer": "new",
                "shipping_fee": float(o.get("actual_shipping_fee", 0) or 0),
                "platform_fee": 0,
                "created_at_sp": local.isoformat() if local else "",
                "buyer_key": _buyer_key(addr),
                "payment_method": o.get("payment_method", "") or "",
            })
            for line, it in enumerate(o.get("item_list") or []):
                iid = it.get("item_id")
                meta = item_meta.get(iid, {})
                sku = it.get("model_sku") or it.get("item_sku") or meta.get("sku") or str(iid)
                qty = int(it.get("model_quantity_purchased", 1) or 1)
                price = float(it.get("model_discounted_price",
                                     it.get("model_original_price", 0)) or 0)
                items.append({
                    "order_id": osn, "line": line, "sku": sku,
                    "name": it.get("item_name") or meta.get("name") or sku,
                    "category": meta.get("category", ""),
                    "qty": qty, "price": price, "cost": 0,
                })
    return orders, items


def run_sync(force_days=None):
    if not (PARTNER_ID and PARTNER_KEY):
        raise RuntimeError("ยังไม่ได้ตั้ง SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY")
    deadline = time.time() + TIME_BUDGET
    item_meta, _pinfo = sync_products()          # อัปเดตสินค้า + ได้ map หมวด
    state = _load_state()
    access, shop_id = _ensure_token(state)

    now = int(time.time())
    last = state.get("last_sync_time")
    if force_days:
        t_from = now - int(force_days) * 86400
    elif last:
        try:
            dt = datetime.datetime.fromisoformat(last.replace("Z", ""))
            t_from = int(dt.timestamp()) - 86400      # เผื่อ 1 วันกันตกหล่น
        except Exception:
            t_from = now - 7 * 86400
    else:
        t_from = now - int(os.environ.get("INITIAL_SYNC_DAYS", "365")) * 86400

    all_sns = []
    ws = t_from
    while ws < now and time.time() < deadline:
        we = min(ws + WINDOW, now)
        all_sns += _order_sns(access, shop_id, ws, we, deadline)
        ws = we
    all_sns = list(dict.fromkeys(all_sns))         # ไม่ซ้ำ

    orders, items = _build_rows(access, shop_id, all_sns, item_meta)
    if items:
        sb_delete_items([o["order_id"] for o in orders])
    sb_upsert("sp_orders", orders, "order_id")
    sb_upsert("sp_order_items", items, "order_id,line")
    sb_upsert("sp_sync", [{"id": 1, "last_sync_time": datetime.datetime.utcnow().isoformat(),
                           "updated_at": datetime.datetime.utcnow().isoformat()}], "id")
    return {"platform": "shopee", "synced_orders": len(orders),
            "synced_items": len(items), "products": _pinfo.get("products", 0),
            "from": datetime.datetime.utcfromtimestamp(t_from + TH_OFFSET).strftime("%Y-%m-%d"),
            "partial": time.time() >= deadline}


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
            if qs.get("products", ["0"])[0] in ("1", "true", "yes"):
                _meta, info = sync_products()
                self._send(200, {"platform": "shopee", **info})
                return
            self._send(200, run_sync(force_days=qs.get("days", [None])[0]))
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
