# ตั้งค่า Supabase + Sync (เก็บข้อมูลใน DB, อัปเดตเฉพาะใหม่)

## A. สร้าง Supabase
1. supabase.com → **New project** (ชื่อ seller-dashboard, ตั้ง DB password) → Create (รอ ~2 นาที)
2. **SQL Editor → New query** → วางทั้งหมดจาก `db/schema.sql` → **Run**
3. **Settings → API** → ก๊อปเก็บไว้:
   - **Project URL** (เช่น https://xxxx.supabase.co)
   - **service_role** key (อันลับ ใต้ "Project API keys")

## B. ใส่ Environment Variables ใน Vercel
Project → Settings → Environment Variables → เพิ่ม แล้ว **Redeploy**:
| Name | Value |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_KEY` | service_role key |
| `CRON_SECRET` | สุ่มอักษรอะไรก็ได้ (กันคนอื่นสั่ง sync) |
| `INITIAL_SYNC_DAYS` | 90 (ครั้งแรกดึงย้อนหลังกี่วัน — เริ่ม 90 กันค้าง) |

## C. Push code → Vercel deploy

## D. Sync ครั้งแรก (กดเอง 1 ครั้ง)
เปิดในเบราว์เซอร์:
```
https://seller-dashboard-next.vercel.app/api/sync?pw=รหัสผ่านของคุณ
```
รอ ~5-15 วิ ถ้าได้ `{"ok":true,"synced_orders":N,...}` = สำเร็จ
(ถ้าขึ้น timeout/error ให้ลด INITIAL_SYNC_DAYS เป็น 60 แล้วลองใหม่)

## E. เสร็จ
- `/api/data` จะอ่านจาก Supabase อัตโนมัติ (เร็ว ไม่ติด timeout)
- Cron sync ออเดอร์ใหม่ทุกวันตี 1 (ปรับใน vercel.json ได้)
- อยาก sync ตอนนี้: เปิด URL `/api/sync?pw=...` เมื่อไหร่ก็ได้
