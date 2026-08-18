# AGENTS.md — Flowku Bot (Hermes Agent & Developer Context)

Panduan teknis dan instruksi operasi untuk **Hermes Agent** di VPS Flowku Bot mengenai integrasi **Chatin Custom Bot Logic**.

---

## 📌 Context: Flowku Bot + Chatin Integration

Flowku Bot berfungsi sebagai **Custom Bot Logic Engine** untuk platform **Chatin** (`chatin.coderey.dev`). 

Ketika pesan WhatsApp masuk ke nomor bisnis Flowku di Chatin:
1. Chatin mendeteksi mode `custom` dan mem-forward payload pesan ke endpoint `/chatin/process` di Flowku Bot (Port 8700).
2. Flowku Bot memproses transaksi (pencatatan otomatis, Gemini OCR struk, laporan, dll) dan mengembalikan JSON `{ "reply": "..." }`.
3. Chatin menerima balasan tersebut dan mengirimkannya kembali ke WhatsApp pengguna via KirimDev SDK / Meta Cloud API.

---

## ⚙️ Environment Variables (`.env`)

Pastikan file `.env` di VPS memasukkan variabel rahasia ini:

```env
# Chatin Webhook Secret Authentication
CHATIN_WEBHOOK_SECRET=chsec_flowku_secret_2026
```

---

## 🚀 Langkah Deploy / Update di VPS (Hermes Agent Checklist)

Saat menerima instruksi deploy atau update dari GitHub:

```bash
# 1. Masuk ke direktori flowku-bot di VPS
cd /home/ubuntu/flowku-bot

# 2. Pull kode terbaru dari branch main
git pull origin main

# 3. Pastikan CHATIN_WEBHOOK_SECRET di .env sudah diset
grep CHATIN_WEBHOOK_SECRET .env || echo "CHATIN_WEBHOOK_SECRET=chsec_flowku_secret_2026" >> .env

# 4. Restart service FastAPI / Uvicorn (PM2 atau systemd)
pm2 restart flowku-bot
# atau jika menggunakan uvicorn langsung / systemctl:
# systemctl restart flowku-bot
```

---

## 📡 API Endpoint: `POST /chatin/process`

- **URL**: `http://localhost:8700/chatin/process` (atau domain `https://.../chatin/process`)
- **Headers**:
  - `Content-Type: application/json`
  - `X-Chatin-Secret: <CHATIN_WEBHOOK_SECRET>`
- **Payload Request**:
  ```json
  {
    "phone": "6285156266871",
    "text": "50rb makan siang",
    "type": "text",
    "contact_name": "Reynaldi",
    "customer_id": "cus_BR11YSEDFGY34Z7T2J91MPJBAM",
    "message_id": "wamid.HBgN...",
    "timestamp": "2026-08-18T19:30:00Z"
  }
  ```
- **Payload Response**:
  ```json
  {
    "reply": "✅ Pengeluaran Rp50.000 (makan siang) berhasil dicatat!\n📂 Kategori: Makanan"
  }
  ```

---

## 🧪 Cara Pengujian Manual (cURL Test)

Jalankan perintah ini di VPS untuk menguji endpoint secara langsung:

```bash
curl -X POST http://localhost:8700/chatin/process \
  -H "Content-Type: application/json" \
  -H "X-Chatin-Secret: chsec_flowku_secret_2026" \
  -d '{
    "phone": "6285156266871",
    "text": "50rb makan siang",
    "type": "text"
  }'
```

*Ekspektasi Output*:
`{"reply":"✅ Pengeluaran Rp50.000 (makan siang) berhasil dicatat!\n📂 Kategori: Makanan"}`

---

## 📁 Key Files Reference
- `config.py`: Konfigurasi variabel lingkungan (termasuk `CHATIN_WEBHOOK_SECRET`).
- `main.py`: Endpoint `@app.post("/chatin/process")` & routing webhook.
- `parser.py`: Logika ekstraksi transaksi & kata kunci pengeluaran/pemasukan.
- `ocr.py`: Integrasi Gemini 2.5 Flash Vision OCR untuk membaca foto struk belanja.
- `firestore_db.py`: Database helper Firestore untuk menyimpan catatan keuangan user.
