# Seller Dashboard — Next (Vercel)

หน้าเว็บ dashboard + backend ดึง Lazada สด รันบน Vercel (auto-deploy จาก GitHub)

## โครงสร้าง
```
index.html          ← หน้า dashboard (login แล้วดึงจาก /api/data)
api/data.py         ← Python serverless: ดึง Lazada สด คืน JSON (เช็ครหัสผ่าน)
requirements.txt    ← requests
```

## Deploy
1. push 3 ไฟล์นี้ขึ้น GitHub repo
2. vercel.com → Add New Project → import repo นี้ → Framework = **Other**
3. ตั้ง **Environment Variables**:
   - `DASH_PASSWORD` = รหัสผ่านเข้าหน้าเว็บ
   - `LZ_APP_KEY` = 139163
   - `LZ_APP_SECRET` = (App Secret ของ Lazada)
   - `LZ_REFRESH_TOKEN` = (refresh_token จาก auth.py / config.json)
4. Deploy → ได้ลิงก์ `https://xxx.vercel.app`

ข้อมูล cache ฝั่ง CDN 15 นาที (ดึง Lazada ใหม่อัตโนมัติเมื่อหมดอายุ)
token หมดอายุ (~30 วัน) ให้รัน `auth.py lazada` ใหม่แล้วอัปเดต `LZ_REFRESH_TOKEN`
