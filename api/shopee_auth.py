"""
Vercel function: /api/shopee_auth
ขั้นตอนอนุญาตให้แอปเข้าถึงร้าน Shopee (OAuth ของ Shopee Open Platform API v2)

วิธีใช้ (ทำครั้งเดียวตอนตั้งค่า):
  1) ขอลิงก์อนุญาต:  /api/shopee_auth?pw=<DASH_PASSWORD>&link=1
     -> ได้ auth_url  เปิดในเบราว์เซอร์ แล้วล็อกอินร้าน Shopee กดอนุญาต
  2) Shopee จะ redirect กลับมาที่ /api/shopee_auth?code=...&shop_id=...
     -> โค้ดนี้จะแลก code เป็น access_token + refresh_token แล้วเก็บลงตาราง sp_sync

Environment Variables ที่ต้องมี:
  SHOPEE_PARTNER_ID, SHOPEE_PARTNER_KEY     (จาก Shopee Open Platform console)
  SHOPEE_REDIRECT                            (URL ของ endpoint นี้ เช่น https://<app>.vercel.app/api/shopee_auth)
  DASH_PASSWORD, SUPABASE_URL, SUPABASE_SERVICE_KEY
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

# โฮสต์กลางของ Shopee (ใช้ได้ทุกภูมิภาครวมไทย); sandbox ใช้ partner.test-stable.shopeemobile.com
SHOPEE_HOST = os.environ.get("SHOPEE_HOST", "https://partner.shopeemobile.com").rstrip("/")
PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID", "")
PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")
REDIRECT = os.environ.get("SHOPEE_REDIRECT", "")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _sign_public(path, ts):
    """ลายเซ็นสำหรับ public API (auth/token): base = partner_id + path + timestamp"""
    base = f"{PARTNER_ID}{path}{ts}".encode()
    return hmac.new(PARTNER_KEY.encode(), base, hashlib.sha256).hexdigest()


def _sb_headers():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json"}


def _sb_upsert(table, rows, on_conflict):
    r = requests.post(f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                      headers={**_sb_headers(),
                               "Prefer": "resolution=merge-duplicates,return=minimal"},
                      json=rows, timeout=25)
    r.raise_for_status()


def auth_link():
    """สร้าง URL ให้ผู้ขายกดอนุญาตร้าน"""
    if not (PARTNER_ID and PARTNER_KEY and REDIRECT):
        return {"error": "ยังไม่ได้ตั้ง SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY / SHOPEE_REDIRECT"}
    path = "/api/v2/shop/auth_partner"
    ts = int(time.time())
    sign = _sign_public(path, ts)
    q = urllib.parse.urlencode({"partner_id": PARTNER_ID, "timestamp": ts,
                                "sign": sign, "redirect": REDIRECT})
    return {"auth_url": f"{SHOPEE_HOST}{path}?{q}",
            "note": "เปิดลิงก์นี้ในเบราว์เซอร์ ล็อกอินร้าน Shopee แล้วกดอนุญาต"}


def exchange_code(code, shop_id):
    """แลก code -> access_token + refresh_token แล้วเก็บลง sp_sync"""
    path = "/api/v2/auth/token/get"
    ts = int(time.time())
    sign = _sign_public(path, ts)
    url = f"{SHOPEE_HOST}{path}?" + urllib.parse.urlencode(
        {"partner_id": PARTNER_ID, "timestamp": ts, "sign": sign})
    body = {"code": code, "shop_id": int(shop_id), "partner_id": int(PARTNER_ID)}
    r = requests.post(url, json=body, timeout=25)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(f"token/get error: {d.get('error')} {d.get('message')}")
    access = d.get("access_token")
    refresh = d.get("refresh_token")
    expire_in = int(d.get("expire_in", 14400) or 14400)
    if not access or not refresh:
        raise RuntimeError(f"ไม่ได้รับ token: {d}")
    now = datetime.datetime.utcnow()
    expire_at = now + datetime.timedelta(seconds=expire_in)
    _sb_upsert("sp_sync", [{
        "id": 1, "shop_id": str(shop_id),
        "access_token": access, "refresh_token": refresh,
        "token_expire": expire_at.isoformat(),
        "updated_at": now.isoformat(),
    }], "id")
    return {"ok": True, "shop_id": str(shop_id), "expire_in": expire_in}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        shop_id = qs.get("shop_id", [None])[0]
        want_link = qs.get("link", ["0"])[0] in ("1", "true", "yes")
        pw = qs.get("pw", [""])[0]

        # --- callback จาก Shopee: มี code + shop_id (ไม่มี pw) ---
        if code and shop_id:
            try:
                if not SB_URL or not SB_KEY:
                    raise RuntimeError("ยังไม่ได้ตั้ง SUPABASE_URL / SUPABASE_SERVICE_KEY")
                exchange_code(code, shop_id)
                self._html(200,
                           "<h2>✅ เชื่อมต่อ Shopee สำเร็จ</h2>"
                           f"<p>shop_id: {shop_id}</p>"
                           "<p>เก็บ token เรียบร้อย — ปิดหน้านี้แล้วสั่ง sync ได้เลยที่ "
                           "<code>/api/shopee_sync?pw=...</code></p>")
            except Exception as e:
                self._html(500, f"<h2>❌ เชื่อมต่อไม่สำเร็จ</h2><pre>{e}</pre>")
            return

        # --- ขอลิงก์อนุญาต (ต้องมีรหัสผ่าน) ---
        if pw != os.environ.get("DASH_PASSWORD", ""):
            self._json(401, {"error": "unauthorized"})
            return
        if want_link:
            self._json(200, auth_link())
            return
        self._json(200, {"usage": "เรียก ?pw=<รหัส>&link=1 เพื่อขอลิงก์อนุญาตร้าน Shopee"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, html):
        body = ("<!doctype html><meta charset=utf-8>"
                "<body style='font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px'>"
                + html + "</body>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
