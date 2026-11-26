# BotTread Console

## Objective

หน้าเว็บ (Next.js) สำหรับ**ดูสถานะ** BotTread บน Linux server แทนแอป Windows WPF (`app/`): นับถอยหลังรอบถัดไป, 7 สมาชิกทีม, แผนเทรด, พอร์ต (เปิดอยู่/ปิดแล้ว/คำสั่ง/log), ค่า AI, Kafka และดูรูปกราฟ PNG
**หน้านี้ไม่แตะ Binance และไม่รัน `team.py` เอง** — อ่านไฟล์ใน `cache/` และ `reports/` อย่างเดียว ยกเว้นสร้างไฟล์ว่างใน `cache/requests/` (4 ชนิด + `close-<id>` ของปุ่มปิดไม้) ให้ตัวรัน (runner) ทำแทน

## Workflow

```
ตัวรัน (runner) ──เขียนทุก 5 วิ──▶ cache/runner.json   ──▶ console อ่าน (เก่ากว่า 30 วิ = "ตัวรันไม่ทำงาน")
team.py ──────────────────────────▶ cache/*.json, team.log, reports/**.png ──▶ console อ่าน
console ──ปุ่ม──▶ cache/requests/{run|stop|portfolio|kafka|close-<id>}   (ไฟล์ว่าง, ตัวรันลบเองเมื่อทำแล้ว)
```

| ปุ่ม | ไฟล์ที่เขียน | หมายเหตุ |
|---|---|---|
| ทำงาน | `run` | รัน 1 รอบเดี๋ยวนี้ (ปฏิเสธ 503 ถ้าตัวรันไม่ทำงาน) |
| หยุด | `stop` | ต้องกดยืนยัน (ปฏิเสธ 503 ถ้าตัวรันไม่ทำงาน) |
| รีเฟรชพอร์ต | `portfolio` | เว้นอย่างน้อย 10 วิ |
| แท็บ Kafka | `kafka` | เปิดแท็บค้างไว้ = ขอทุก ~30 วิ |
| ปิดไม้ (แท็บพอร์ต ทีละไม้) | `close-<id>` | ต้องกดยืนยัน; เฉพาะไม้ใน `portfolio.json` ที่อยู่บน Binance และไม่มีคำสั่งไม่ชัด (409 ถ้าไม่ใช่, 503 ถ้าตัวรันไม่ทำงาน); ตัวรันเรียก `team.py close <id>` ระหว่างรอบเท่านั้น = ปิดทั้งไม้ราคาตลาด reduceOnly ผลลงแชตทีม |

- ล็อกอินด้วยรหัสเดียว: คุกกี้ HttpOnly + SameSite=Strict ลงลายเซ็น HMAC อายุ 7 วัน; ผิด 5 ครั้งล็อก 5 นาที (ในหน่วยความจำ)
- ทุกหน้าและทุก API ต้องมี session ยกเว้นหน้า/API ล็อกอิน และเครื่องในวง LAN; หน้าที่ไม่มี session แสดงหน้าล็อกอิน (rewrite ไม่ใช่ redirect)
- เครื่องในวง LAN ไม่ต้องใส่รหัส (ทั้งดูและกดปุ่ม): ที่อยู่ของการเชื่อมต่อเป็น `192.168.x.x` และเปิดด้วย IP ของเครื่องทีม เช่น `http://192.168.x.x:3300` — ที่อยู่อ่านจาก socket (`socket-ip.cjs` โหลดก่อน `server.js`) ไม่เชื่อ `X-Forwarded-For` ที่ผู้เรียกส่งมา; เปิดด้วยชื่อ host หรือมาจากที่อยู่อื่น = ถามรหัสตามเดิม
- `/api/request` รับเฉพาะ `Content-Type: application/json` (415): SameSite=Strict ยังส่งคุกกี้จาก port อื่นของ host เดียวกัน แต่หน้าเว็บที่อื่นส่ง JSON ไม่ได้ถ้าไม่ผ่าน preflight
- รูปกราฟ: ให้เฉพาะ PNG ใต้ `reports/` (ตรวจ signature + path ไม่หลุดโฟลเดอร์)
- เงินทุกค่าเป็นสตริงทศนิยม คำนวณด้วย BigInt ไม่ใช้ float

## Config

ตัวแปรสภาพแวดล้อม (ตัวอย่างค่าปลอม: `.env.example`):

| ตัวแปร | จำเป็น | ค่าเริ่มต้น | ความหมาย |
|---|---|---|---|
| `BOTTREAD_ROOT` | ใช่ | - | โฟลเดอร์ที่มี `cache/` และ `reports/` (ใน Docker = `/data`) |
| `CONSOLE_PASSWORD` | ใช่ | - | รหัสผ่านล็อกอิน |
| `CONSOLE_SECRET` | ใช่ | - | กุญแจเซ็นคุกกี้ ยาว >= 16 ตัวอักษร สุ่ม |
| `PORT` | ไม่ | `3300` | พอร์ตที่ฟัง (ฟังที่ `0.0.0.0`) |
| `LOG_TZ_OFFSET_MIN` | ไม่ | `420` | เขตเวลาของเครื่องที่เขียน `team.log` (นาทีตะวันออกของ UTC; UTC=0) |
| `TRUST_PROXY` | ไม่ | `0` | `1` = มี reverse proxy ตัวเดียวหน้าสุด: นับล็อกแยกตาม IP จาก `X-Forwarded-For` |
| `COOKIE_SECURE` | ไม่ | `0` | `1` = บังคับ flag Secure (ถ้า proxy ส่ง `X-Forwarded-Proto: https` จะเปิดให้เองอยู่แล้ว) |

เวลาทุกจอแสดงเป็น Asia/Bangkok (ค.ศ.) คงที่

## Dependency

- Node 24, Next.js 16.4.2 (App Router), React 19.3.1, TypeScript — ไม่มี dependency อื่น (ไม่มีไลบรารีกราฟ/UI)
- ต้องมีตัวรันที่เขียน `cache/runner.json` และลบไฟล์ใน `cache/requests/` (ตามสัญญาเดิมของแอป WPF)
- Docker (ถ้า deploy เป็น container)

## Usage example

พัฒนา:

```bash
cd console
cp .env.example .env.local        # แก้เป็นค่าจริงของเครื่องตัวเอง (ไฟล์นี้ถูก git ignore)
npm ci
npm run dev                       # http://localhost:3300
npm run typecheck && npm test && npm run build
```

Docker:

```bash
docker build -t bottread-console ./console
docker run -d --name bottread-console --restart unless-stopped \
  -p 3300:3300 \
  -v /srv/bottread:/data \
  -e BOTTREAD_ROOT=/data \
  -e CONSOLE_PASSWORD="$(cat /run/secrets/console_password)" \
  -e CONSOLE_SECRET="$(cat /run/secrets/console_secret)" \
  -e TRUST_PROXY=1 -e LOG_TZ_OFFSET_MIN=0 \
  bottread-console
```

`/srv/bottread` ต้องมี `cache/` และ `reports/` ของ BotTread — ใส่ HTTPS ด้วย reverse proxy (ต้องส่ง `X-Forwarded-Proto` และ `X-Forwarded-For`)

## Limitation

- container รันเป็น `node` (uid 1000) — uid นี้ต้องเขียน `cache/requests/` ได้ (ตั้งให้ตรงกับผู้ใช้ของตัวรัน)
- ไม่ตั้ง `TRUST_PROXY=1` = ที่ล็อกอินผิดใช้ตัวนับล็อกร่วมกันทั้งระบบ (คนนอกกดผิด 5 ครั้งล็อกเจ้าของได้ 5 นาที)
- ในวง LAN ไม่มีรหัสกั้น: ทุกเครื่อง/มือถือที่ต่อวง `192.168.x.x` เดียวกันเปิดดูและกดปุ่มปิดไม้/เริ่มรอบได้ · ตั้ง `TRUST_PROXY=1` = ถามรหัสทุกเครื่อง (socket เป็นของ proxy) · อย่าวาง proxy/tunnel ที่อยู่ในวง LAN ไว้หน้า console โดยไม่ตั้งค่านี้ (คนนอกจะถูกนับเป็น LAN) · `next dev` ไม่มี `socket-ip.cjs` = ถามรหัสเสมอ
- ตัวนับล็อกอินผิดอยู่ในหน่วยความจำ: รีสตาร์ตแล้วล้าง, รันหลายอินสแตนซ์แยกกัน
- การแจ้งเตือนในหน้าเว็บเทียบกับข้อมูลที่เห็นล่าสุด — โหลดหน้าใหม่แล้วเริ่มเทียบใหม่
- ไม่มีตัวเลขคิวค้างของ Kafka (ไม่อยู่ในสัญญา `runner.json`) และไม่มี: เริ่มพร้อม Windows, เปิดโฟลเดอร์ reports, single-instance
- `LOG_TZ_OFFSET_MIN` ผิด = เวลาใน log เพี้ยนไปเป็นชั่วโมง
- ไม่มีหน้าแก้ค่า/เปิดไม้; คำสั่งซื้อขายทางเดียวคือปุ่มปิดไม้ (ผ่านตัวรัน → `team.py close` → server สั่งปิด)
