# ECO Bee Farmer

Backend สำหรับแพลตฟอร์ม Precision Apiculture ใช้ Python standard library และ SQLite จึงเริ่มได้ทันทีโดยไม่ต้องติดตั้งแพ็กเกจ

## เริ่มระบบ

```bash
python3 server.py
```

เปิดระบบได้จากหน้าที่แยกตามผู้ใช้งานดังนี้

- Farmer: `http://127.0.0.1:8000/farmer`
- Organization: `http://127.0.0.1:8000/organization`
- Trace: `http://127.0.0.1:8000/trace`

หน้า `/` จะเปิด Farmer เป็นค่าเริ่มต้น และข้อมูลทั้งหมดเก็บใน `ecobee.db`

บัญชี Organization ใช้อีเมล `admin@ecobee.go.th` และรหัสผ่านจากตัวแปร `ECOBEE_ADMIN_PASSWORD` หากรันในเครื่องโดยไม่กำหนด ระบบจะสร้างรหัสแบบใช้ครั้งแรกและแสดงใน terminal

## รันแบบ Production

```bash
ECOBEE_ENV=production \
ECOBEE_HOST=0.0.0.0 \
ECOBEE_ADMIN_PASSWORD='รหัสผ่านยาวอย่างน้อย12ตัว' \
ECOBEE_DB=/var/data/ecobee.db \
python3 server.py
```

ไฟล์ `render.yaml` เตรียม Web Service, health check, environment และ persistent disk สำหรับ Render แล้ว ห้าม commit ไฟล์ `.env` หรือฐานข้อมูลขึ้น Git

สำรองฐานข้อมูลแบบ consistent snapshot:

```bash
python3 scripts/backup.py --database /var/data/ecobee.db --output-dir /var/backups
```

## REST API หลัก

- `POST /api/auth/register`, `POST /api/auth/login`
- `POST /api/org/auth/login`, `GET /api/org/farmers`
- `GET|POST /api/hives` — รังและรัศมีตามสายพันธุ์
- `POST /api/plants` — Ground truth ของพรรณไม้/เดือนออกดอก/geometry
- `POST /api/risk-zones` — พื้นที่ปลอดภัยหรือเสี่ยงสารเคมี
- `POST /api/assess` — ประเมินอาหาร 12 เดือน ความเสี่ยง และ Virtual Pin
- `POST /api/movements` — Check-in และประวัติย้ายรัง
- `POST /api/harvests`, `GET /api/trace/{batch_code}` — lot และการตรวจสอบย้อนกลับ

ทุก endpoint หลัง login ใช้ header `Authorization: Bearer <token>` ยกเว้น health, trace และ storage adapter ของหน้าเว็บ

ตัวอย่าง Virtual Pin:

```bash
curl -X POST http://127.0.0.1:8000/api/assess \
  -H 'Authorization: Bearer TOKEN' -H 'Content-Type: application/json' \
  -d '{"lat":13.5282,"lng":99.8134,"species":"cerana"}'
```
