"""
OCR Service — Extract text dari foto struk
Mendukung 3 backend (urutan prioritas):
  1. Gemini Vision API (paling akurat, structured extraction)
  2. Google Cloud Vision API (butuh credentials)
  3. Tesseract OCR (lokal, gratis, juga dipakai untuk pre-check)
"""
import logging
import httpx
import base64
import io
import json
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── GEMINI OCR (primary) ──
USE_GEMINI = False
try:
    from google import genai
    from config import GEMINI_API_KEY, GEMINI_MODELS
    if GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        USE_GEMINI = True
        logger.info(f"Gemini OCR available — models: {', '.join(GEMINI_MODELS)}")
    else:
        logger.info("GEMINI_API_KEY not set, skipping Gemini OCR")
except Exception as e:
    logger.info(f"Gemini not available: {e}")

# ── GOOGLE VISION (fallback) ──
USE_GOOGLE_VISION = False
try:
    from google.cloud import vision
    USE_GOOGLE_VISION = True
    logger.info("Using Google Cloud Vision for OCR")
except Exception:
    logger.info("Google Vision not available, using Tesseract")


# ════════════════════════════════════════════
# GEMINI MODEL FALLBACK HELPER
# ════════════════════════════════════════════

def _gemini_generate(contents: list, models: list = None) -> str:
    """
    Try Gemini API with model fallback chain.
    Returns response text from first successful model.
    Raises last exception if all models fail.
    """
    models = models or GEMINI_MODELS
    last_err = None

    for model in models:
        try:
            response = _gemini_client.models.generate_content(
                model=model,
                contents=contents,
            )
            return response.text.strip()
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                logger.warning(f"Gemini {model} rate limited (429) — trying next model")
            elif "not found" in err_str.lower() or "404" in err_str:
                logger.warning(f"Gemini {model} not available — trying next model")
            else:
                logger.warning(f"Gemini {model} error: {e} — trying next model")
            last_err = e
            continue

    # All models failed
    raise last_err or Exception("All Gemini models failed")


# ════════════════════════════════════════════
# IMAGE DOWNLOAD & PREPROCESSING
# ════════════════════════════════════════════

async def download_image(url: str, save_path: str = "/tmp/waha_media") -> str | None:
    """Download image dari WAHA media URL dan simpan lokal."""
    Path(save_path).mkdir(parents=True, exist_ok=True)
    try:
        headers = {}
        if "localhost:3000" in url or "127.0.0.1:3000" in url:
            api_key = os.getenv("WAHA_API_KEY", "")
            if not api_key:
                try:
                    with open(os.path.expanduser("~/waha/.env")) as f:
                        for line in f:
                            if line.startswith("WAHA_API_KEY="):
                                api_key = line.strip().split("=", 1)[1]
                                break
                except Exception:
                    pass
            if api_key:
                headers["x-api-key"] = api_key

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "image/jpeg")
                ext = ".jpg"
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                filepath = f"{save_path}/receipt_{hash(url)}{ext}"
                with open(filepath, "wb") as f:
                    f.write(resp.content)
                logger.info(f"Image saved to {filepath}")
                return filepath
    except Exception as e:
        logger.error(f"Error downloading image: {e}")
    return None


def preprocess_receipt_image(image_path: str) -> "Image":
    """Preprocessing gambar struk untuk OCR lebih akurat."""
    from PIL import Image, ImageFilter, ImageEnhance

    img = Image.open(image_path)
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")

    width, height = img.size
    min_width = 1000
    if width < min_width:
        scale = min_width / width
        new_size = (int(width * scale), int(height * scale))
        img = img.resize(new_size, Image.LANCZOS)

    img = img.convert("L")
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(2.0)
    img = img.filter(ImageFilter.MedianFilter(size=3))

    try:
        import numpy as np
        img_array = np.array(img)
        from PIL import Image as PILImage
        block_size = 31
        padded = np.pad(img_array, block_size // 2, mode='edge')
        local_mean = np.zeros_like(img_array, dtype=float)
        for i in range(block_size):
            for j in range(block_size):
                local_mean += padded[i:i + img_array.shape[0], j:j + img_array.shape[1]]
        local_mean /= block_size * block_size
        offset = 10
        binary = ((img_array > local_mean - offset) * 255).astype(np.uint8)
        img = PILImage.fromarray(binary)
    except ImportError:
        img = img.point(lambda x: 0 if x < 128 else 255, '1').convert('L')

    return img


def _local_path_from_input(image_url: str, base64_data: str = None) -> str | None:
    """Resolve image dari URL atau base64 ke local file path."""
    local_path = None

    if base64_data:
        try:
            import tempfile
            if "," in base64_data:
                base64_data = base64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(base64_data)
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))
            ext = ".jpg"
            if img.format == "PNG":
                ext = ".png"
            elif img.format == "WEBP":
                ext = ".webp"
            tmp_file = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="receipt_", dir="/tmp/waha_media", delete=False
            )
            tmp_file.write(img_bytes)
            tmp_file.close()
            local_path = tmp_file.name
            logger.info(f"Saved base64 image to {local_path}")
        except Exception as e:
            logger.error(f"Error decoding base64 image: {e}")

    if not local_path and image_url:
        import asyncio
        local_path = asyncio.get_event_loop().run_until_complete(download_image(image_url)) if not asyncio.get_event_loop().is_running() else None

    return local_path


# ════════════════════════════════════════════
# TESSERACT PRE-CHECK
# ════════════════════════════════════════════

def is_receipt_image(image_path: str) -> tuple[bool, str]:
    """
    Pre-check pakai Tesseract: apakah gambar ini struk/nota?
    Returns: (is_receipt: bool, reason: str)
    Cepat & gratis — jaga-jaga sebelum kirim ke Gemini.
    """
    try:
        import pytesseract
        from PIL import Image

        # Pre-check: preprocessing ringan (cepat, tapi cukup untuk struk thermal)
        from PIL import ImageEnhance, ImageFilter
        img = Image.open(image_path)
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        # Resize kecil supaya Tesseract cepat
        w, h = img.size
        if w > 800:
            scale = 800 / w
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.MedianFilter(size=3))

        text = pytesseract.image_to_string(img, lang="ind+eng", config="--oem 3 --psm 6")
        text_clean = text.strip()

        # Terlalu sedikit teks → cek dulu apakah ini gambar foto (kemungkinan struk thermal buram)
        if len(text_clean) < 15:
            # Kalau gambar asli (foto dari HP), kemungkinan struk thermal yang buram
            # Default lolos — biar Gemini yang verifikasi
            try:
                orig = Image.open(image_path)
                w, h = orig.size
                # Foto dari HP biasanya > 300px dan landscape/portrait
                if min(w, h) > 200:
                    logger.info(f"Low text ({len(text_clean)} chars) but photo-sized ({w}x{h}), passing to Gemini")
                    return True, ""
            except Exception:
                pass
            return False, "GAMBAR_TIDAK_ADA_TEKS"

        text_lower = text_clean.lower()

        # ── INDIKATOR STRUK (positif) ──
        receipt_signals = [
            # Harga/mata uang
            "rp", "rupiah", "idr", "total", "subtotal", "harga",
            "bayar", "cash", "kembali", "change", "tunai", "qris",
            # Struk keywords
            "struk", "nota", "receipt", "invoice", "faktur", "bon",
            # Toko/SPBU
            "alfamart", "indomaret", "circle k", "pertamina", "shell",
            "minimarket", "supermarket", "warung", "resto", "kafe",
            # Transaksi
            "kasir", "cashier", "operator", "terima kasih", "thank you",
            "member", "diskon", "promo", "qty", "jumlah",
            # Format angka khas struk (pola harga)
        ]

        signal_count = sum(1 for s in receipt_signals if s in text_lower)

        # Cek pola harga: angka 3+ digit yang bisa jadi harga Rupiah
        import re
        price_patterns = re.findall(r'[\d.,]{4,}', text_clean)
        has_price_pattern = len(price_patterns) >= 2

        # ── INDIKATOR BUKAN STRUK (negatif) ──
        non_receipt_signals = [
            # Chat/messaging
            "whatsapp", "chat", "pesan", "message", "group", "grup",
            "telegram", "discord", "slack", "kirim pesan", "balas",
            # Social media
            "instagram", "tiktok", "youtube", "facebook", "twitter", "threads",
            "like", "follow", "subscribe", "komentar", "comment", "share",
            # Identitas/dokumen
            "ktp", "sim", "passport", "kartu identitas", "npwp", "kk",
            # UI/Screen elements
            "settings", "pengaturan", "menu", "home", "login", "daftar",
            "search", "cari", "filter", "sort", "loading", "error",
            "browser", "chrome", "safari", "firefox", "tab", "bookmark",
            "download", "upload", "file", "folder", "desktop", "taskbar",
            "notification", "notifikasi", "peringatan", "alert",
            # Undangan/acara
            "selamat", "ulang tahun", "wedding", "undangan", "nikah",
            # Game/entertainment
            "game", "level", "score", "play", "pause", "streaming",
            # GPS/Location/Timestamp (foto dokumen, bukan struk)
            "latitude", "longitude", "koordinat", "coordinate", "gps",
            "kecamatan", "kelurahan", "kota", "provinsi", "indonesia",
            "jawa timur", "jawa barat", "jawa tengah", "sumatera", "kalimantan",
            "wib", "wita", "wit", "timestamp", "am", "pm",
            "°", "menit", "detik",
        ]
        negative_count = sum(1 for s in non_receipt_signals if s in text_lower)

        # Keputusan final
        # Lolos kalau ada sinyal struk yang cukup kuat
        if signal_count >= 2:
            return True, ""
        if signal_count >= 1 and has_price_pattern:
            return True, ""

        # Ada sinyal negatif → tolak
        if negative_count >= 1:
            return False, "BUKAN_STRUK"

        # Tidak ada sinyal struk
        if signal_count == 0:
            # Lolos HANYA kalau ada pola harga (angka yang terlihat seperti harga Rupiah)
            # Ini menangani struk thermal yang Tesseract salah baca teksnya tapi angkanya ketangkep
            if has_price_pattern and len(text_clean) > 30:
                logger.info(f"No receipt signals but {len(price_patterns)} price patterns, passing to Gemini")
                return True, ""
            return False, "BUKAN_STRUK"

        return False, "GAMBAR_TIDAK_JELAS"

    except Exception as e:
        logger.warning(f"Pre-check error: {e}, allowing through")
        return True, ""  # Kalau error, jangan blokir


# ════════════════════════════════════════════
# OCR BACKENDS
# ════════════════════════════════════════════

def ocr_gemini(image_path: str) -> str:
    """OCR pakai Gemini Vision API — extract semua teks dari gambar struk."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        text = _gemini_generate(
            contents=[
                "Extract ALL text from this receipt image. Return the raw text exactly as visible, "
                "preserving line breaks and layout. Include store name, items, quantities, prices, "
                "total, date, and any other text. Do NOT add commentary or interpretation.",
                img,
            ],
        )
        logger.info(f"Gemini OCR extracted {len(text)} chars")
        return text
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            logger.warning(f"Gemini OCR rate limited (429) — will fallback")
        else:
            logger.error(f"Gemini OCR error: {e}")
        return ""


def _is_valid_item_name(nama: str) -> bool:
    """
    Validasi apakah nama item terlihat seperti nama produk asli (bukan OCR noise).
    Return False untuk: gibberish, simbol, nama orang, teks acak.
    """
    import re

    nama = nama.strip()
    if len(nama) < 3:
        return False

    # Hitung rasio huruf vs total karakter
    letters = sum(1 for c in nama if c.isalpha())
    total = len(nama)
    letter_ratio = letters / total

    # Terlalu banyak simbol/angka → noise (minimal 60% huruf)
    if letter_ratio < 0.6:
        return False

    # Karakter aneh khas OCR noise — kalau ada 2+ langsung tolak
    noise_chars = set('|[]{}~£±§©®™°¿¡\\/:;=+_()#@!^&*<>')
    noise_count = sum(1 for c in nama if c in noise_chars)
    if noise_count >= 2:
        return False

    # Terlalu pendek setelah bersih (hanya 2 huruf asli)
    clean = re.sub(r'[^a-zA-Z\u00C0-\u024F]', '', nama)
    if len(clean) < 3:
        return False

    # Kata-kata yang bukan item belanja
    non_item_words = [
        'screenshot', 'chat', 'pesan', 'message', 'whatsapp',
        'telegram', 'instagram', 'tiktok', 'facebook',
        'latitude', 'longitude', 'gps', 'koordinat',
        'kecamatan', 'kelurahan', 'kota', 'provinsi',
        'indonesia', 'jawa', 'sumatera', 'kalimantan',
        'timestamp', 'photo', 'video', 'camera',
        'download', 'upload', 'share', 'forward',
    ]
    nama_lower = nama.lower()
    if any(w in nama_lower for w in non_item_words):
        return False

    return True


def ocr_gemini_structured(image_path: str) -> list[dict] | None:
    """
    Gemini OCR tingkat lanjut: langsung parse struk jadi items terstruktur.
    Returns list of {nama, harga, kategori} atau None kalau gagal.
    """
    try:
        from PIL import Image
        img = Image.open(image_path)

        prompt = """CRITICAL: First determine if this image is actually a RECEIPT/NOTA/INVOICE (proof of purchase with itemized products/services and prices).

VALID receipts: store receipts, restaurant bills, e-commerce orders, parking tickets, SPBU/fuel receipts, online transaction screenshots (Shopee, Tokopedia, GoFood, Grab).

NOT receipts (return [] for these):
- Photos with GPS coordinates, timestamps, location data
- Photos of places, buildings, vehicles, people
- Screenshots of apps, maps, chats, social media
- Documents without purchase items (KTP, SIM, certificates)
- Photos with watermarks showing date/time/GPS/location
- Any image where numbers appear to be coordinates (lat/long), timestamps, or IDs — NOT prices

DO NOT invent items from random numbers! If you see GPS coordinates like "-7.297465°" or timestamps like "Friday 10:30 AM WIB" or addresses like "Surabaya Jawa Timur 60245", these are NOT prices. Return [].

If it IS a valid receipt, return ONLY a JSON array (no markdown, no explanation), each element:
{"nama": "item name", "harga": price_as_integer, "kategori": "category"}

Rules:
- harga = integer in Rupiah (no dots/commas), e.g. 25000 not "25.000"
- Skip: total, subtotal, tax, cash, change, payment info, receipt numbers
- kategori must be one of: food, transport, shopping, health, entertainment, bills, education, beauty, home, investment, social, saving, other_expense
- For Indonesian receipts, item names stay in original language
- If quantity shown (e.g. "2x"), still use total price for that line
- Return [] if no clear items found

SPECIAL RULES FOR DISCOUNTS/VOUCHERS/DEDUCTIONS:
- Detect discount, voucher, potongan, promo, cashback, refund, rebate lines
- Set harga to NEGATIVE integer, e.g. "Diskon 5.000" → {"nama": "Diskon Member", "harga": -5000, "kategori": "other_expense"}
- For percentage discounts (e.g. "Diskon 10%"), calculate the actual Rupiah amount from the receipt total/subtotal and use that as negative harga
- Common patterns: "Diskon -Rp5.000", "Voucher -Rp10.000", "Potongan 2.000", "Promo -Rp3.000", "Cashback -Rp5.000"
- Use the ORIGINAL text from the receipt as nama (e.g. "Diskon Member", "Voucher Shopee", "Potongan Promo")
- ALWAYS include discount items even with negative harga — they reduce the total cost

SPECIAL RULES FOR FUEL/BBM:
- If receipt is from SPBU/Pertamina/Shell/BP/Vivo or contains fuel types (Pertalite, Pertamax, Dexlite, Solar, Premium, Turbo, RON 90/92/95/98), set kategori="transport"
- For fuel items, set nama="Bensin [type]" e.g. "Bensin Pertalite", "Bensin Pertamax", "Bensin Shell V-Power"
- If it's a fuel station receipt with a single total (no itemized list), use: {"nama": "Bensin", "harga": total_amount, "kategori": "transport"}

SPECIAL RULES FOR MINIMARKET (Alfamart, Indomaret, Circle K, Lawson, Family Mart):
- DO NOT just set kategori="shopping" for everything! Categorize each item by what it actually IS:
  - Food/snacks (mie instan, roti, coklat, keripik, biskuit) → kategori="food"
  - Drinks (air mineral, kopi, teh, susu, softdrink, jus) → kategori="food"
  - Cigarettes (rokok, Marlboro, Gudang Garam, Sampoerna) → kategori="other_expense"
  - Toiletries (sabun, shampoo, odol, sikat gigi) → kategori="home"
  - Cleaning (deterjen, pembersih, tisu) → kategori="home"
  - Medicine (obat, paracetamol, vitamin, plester) → kategori="health"
  - Electronics/accessories (baterai, charger, kabel) → kategori="shopping"
  - If truly can't identify the item → kategori="shopping"

Example: [{"nama": "Nasi Goreng", "harga": 25000, "kategori": "food"}, {"nama": "Bensin Pertalite", "harga": 50000, "kategori": "transport"}]"""

        raw = _gemini_generate(
            contents=[prompt, img],
        )

        # Clean up: remove markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        items = json.loads(raw)
        if not isinstance(items, list):
            logger.warning("Gemini structured OCR returned non-list, falling back")
            return None

        # Validate & clean items
        valid_cats = {
            "food", "transport", "shopping", "health", "entertainment",
            "bills", "education", "beauty", "home", "investment",
            "social", "saving", "other_expense",
        }
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            nama = str(item.get("nama", "")).strip()
            harga = int(item.get("harga", 0))
            kategori = str(item.get("kategori", "other_expense")).strip().lower()
            if kategori not in valid_cats:
                kategori = "other_expense"
            # Allow negative harga for discounts (but still validate name)
            if nama and (harga > 0 or harga < 0) and _is_valid_item_name(nama):
                # Extra check: negative items should be actual discounts
                if harga < 0:
                    discount_words = ["diskon", "discount", "voucher", "potongan", "promo", "cashback", "refund", "rebate", "potongan harga", "pengurangan"]
                    if not any(w in nama.lower() for w in discount_words):
                        logger.debug(f"Skipping non-discount negative item: {nama}")
                        continue
                cleaned.append({"nama": nama, "harga": harga, "kategori": kategori})

        logger.info(f"Gemini structured OCR: {len(cleaned)} items extracted")
        return cleaned if cleaned else None

    except json.JSONDecodeError as e:
        logger.warning(f"Gemini structured OCR JSON parse error: {e}")
        return None
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            logger.warning(f"Gemini API rate limited (429) — falling back to Tesseract")
        else:
            logger.error(f"Gemini structured OCR error: {e}")
        return None


def ocr_gemini_structured_transfer(image_path: str) -> dict | None:
    """
    Gemini OCR khusus bukti transfer: extract amount, recipient, bank, ref, dll.
    Returns dict {amount, description, date, bankSource, bankTarget,
                  recipientName, accountNumber, referenceCode, confidence}
    atau None kalau gagal.
    """
    try:
        from PIL import Image
        img = Image.open(image_path)

        prompt = """Analyze this bank transfer receipt/screenshot from an Indonesian bank or e-wallet.
Supported: BCA, Mandiri, BNI, BRI, BSI, CIMB, Permata, Danamon, BTN, Jenius, SeaBank,
GoPay, OVO, Dana, ShopeePay, Flip, etc.

Extract and return ONLY a JSON object (no markdown, no explanation):
{
  "amount": <integer in Rupiah, no dots/commas>,
  "description": "<short description of the transfer>",
  "date": "<YYYY-MM-DD format>",
  "bankSource": "<sender bank name or null>",
  "bankTarget": "<recipient bank name or null>",
  "recipientName": "<recipient name or null>",
  "accountNumber": "<account number or null>",
  "referenceCode": "<transaction reference code or null>",
  "confidence": "<high|medium|low>"
}

Rules:
- amount = integer, no dots/commas, e.g. 150000 not "150.000"
- If cannot find amount, set amount to null
- date in ISO format YYYY-MM-DD. If not found, set to null
- bankSource: detect from logo, header, or app name (e.g. "BCA", "Mandiri", "GoPay")
- bankTarget: recipient's bank. If same bank, can be same as bankSource
- recipientName: the person/entity receiving the money
- accountNumber: recipient account number (remove spaces/dashes)
- referenceCode: transaction ID/reference number
- confidence: "high" if amount+recipient found, "medium" if only amount, "low" if barely readable
- Return {} (empty object) if image is NOT a transfer receipt at all"""

        raw = _gemini_generate(
            contents=[prompt, img],
        )

        # Clean markdown blocks
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        data = json.loads(raw)
        if not isinstance(data, dict) or not data:
            logger.warning("Gemini transfer OCR returned empty/non-dict")
            return None

        # Normalize
        result = {
            "amount": int(data.get("amount", 0)) if data.get("amount") else None,
            "description": str(data.get("description", "")).strip() or None,
            "date": str(data.get("date", "")).strip() or None,
            "bankSource": str(data.get("bankSource", "")).strip() or None,
            "bankTarget": str(data.get("bankTarget", "")).strip() or None,
            "recipientName": str(data.get("recipientName", "")).strip() or None,
            "accountNumber": str(data.get("accountNumber", "")).strip() or None,
            "referenceCode": str(data.get("referenceCode", "")).strip() or None,
            "confidence": str(data.get("confidence", "low")).strip(),
        }

        if result["confidence"] not in ("high", "medium", "low"):
            result["confidence"] = "low"

        logger.info(f"Gemini transfer OCR: amount={result['amount']}, bank={result['bankSource']}, conf={result['confidence']}")
        return result

    except json.JSONDecodeError as e:
        logger.warning(f"Gemini transfer OCR JSON parse error: {e}")
        return None
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
            logger.warning(f"Gemini API rate limited (429) — transfer OCR failed")
        else:
            logger.error(f"Gemini transfer OCR error: {e}")
        return None


def ocr_google_vision(image_path: str) -> str:
    """OCR pakai Google Cloud Vision API."""
    client = vision.ImageAnnotatorClient()
    with open(image_path, "rb") as f:
        content = f.read()
    image = vision.Image(content=content)
    response = client.text_detection(image=image)
    texts = response.text_annotations
    if texts:
        return texts[0].description
    return ""


def ocr_tesseract(image_path: str) -> str:
    """OCR pakai Tesseract (lokal) dengan preprocessing."""
    try:
        import pytesseract
        img = preprocess_receipt_image(image_path)
        custom_config = r"--oem 3 --psm 6"
        text = pytesseract.image_to_string(img, lang="ind+eng", config=custom_config)

        if len(text.strip()) < 20:
            logger.info("First OCR pass too short, trying PSM 4")
            custom_config = r"--oem 3 --psm 4"
            text = pytesseract.image_to_string(img, lang="ind+eng", config=custom_config)

        if len(text.strip()) < 20:
            logger.info("Second OCR pass too short, trying raw image")
            from PIL import Image
            raw_img = Image.open(image_path)
            if raw_img.mode != "L":
                raw_img = raw_img.convert("L")
            text = pytesseract.image_to_string(raw_img, lang="ind+eng", config=custom_config)

        return text
    except Exception as e:
        logger.error(f"Tesseract OCR error: {e}")
        return ""


# ════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════

async def _resolve_local_path(image_url: str, base64_data: str = None) -> str | None:
    """Resolve image input ke local file path."""
    local_path = None

    if base64_data:
        try:
            import tempfile
            if "," in base64_data:
                base64_data = base64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(base64_data)
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))
            ext = ".jpg"
            if img.format == "PNG":
                ext = ".png"
            elif img.format == "WEBP":
                ext = ".webp"
            tmp_file = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="receipt_", dir="/tmp/waha_media", delete=False
            )
            tmp_file.write(img_bytes)
            tmp_file.close()
            local_path = tmp_file.name
            logger.info(f"Saved base64 image to {local_path}")
        except Exception as e:
            logger.error(f"Error decoding base64 image: {e}")

    if not local_path and image_url:
        local_path = await download_image(image_url)

    return local_path


async def extract_text_from_image(image_url: str, base64_data: str = None) -> str:
    """Extract text dari URL gambar atau base64 data. Auto-detect backend."""
    local_path = await _resolve_local_path(image_url, base64_data)
    if not local_path:
        logger.error("No image data available")
        return ""

    # Gemini → Google Vision → Tesseract
    if USE_GEMINI:
        try:
            text = ocr_gemini(local_path)
            if text.strip():
                return text
        except Exception as e:
            logger.warning(f"Gemini OCR failed: {e}")

    if USE_GOOGLE_VISION:
        try:
            text = ocr_google_vision(local_path)
            if text.strip():
                return text
        except Exception as e:
            logger.warning(f"Google Vision failed: {e}")

    return ocr_tesseract(local_path)


async def extract_items_from_image(image_url: str, base64_data: str = None) -> dict:
    """
    Full pipeline: pre-check → structured OCR.
    Returns: {is_receipt: bool, items: list, reason: str, local_path: str}
    """
    result = {"is_receipt": False, "items": None, "reason": "", "local_path": None}

    local_path = await _resolve_local_path(image_url, base64_data)
    if not local_path:
        result["reason"] = "Gagal download gambar"
        return result
    result["local_path"] = local_path

    # ── STEP 1: Tesseract pre-check (cepat & gratis) ──
    is_ok, reason = is_receipt_image(local_path)
    if not is_ok:
        result["reason"] = reason
        logger.info(f"Pre-check REJECTED: {reason}")
        return result
    logger.info("Pre-check PASSED — proceeding to Gemini structured OCR")

    # ── STEP 2: Gemini structured extraction (akurat) ──
    if USE_GEMINI:
        items = ocr_gemini_structured(local_path)
        if items:
            result["is_receipt"] = True
            result["items"] = items
            return result

    # ── STEP 3: Fallback — Tesseract text + regex parsing ──
    text = ocr_tesseract(local_path)
    if text.strip():
        result["is_receipt"] = True
        result["items"] = None  # Caller will use text + parse_ocr_items
        result["reason"] = text  # Reuse reason field for raw text fallback
        return result

    result["reason"] = "Gagal membaca teks dari gambar"
    return result


async def extract_transfer_from_image(image_url: str = None, base64_data: str = None) -> dict:
    """
    Full pipeline untuk bukti transfer.
    Returns: {success: bool, data: dict|None, raw_text: str, reason: str}
    """
    result = {"success": False, "data": None, "raw_text": "", "reason": ""}

    local_path = await _resolve_local_path(image_url, base64_data)
    if not local_path:
        result["reason"] = "Gagal download gambar"
        return result

    # ── STEP 1: Gemini structured transfer OCR ──
    if USE_GEMINI:
        data = ocr_gemini_structured_transfer(local_path)
        if data:
            result["success"] = True
            result["data"] = data
            return result

    # ── STEP 2: Fallback — Gemini raw text + regex parsing ──
    if USE_GEMINI:
        text = ocr_gemini(local_path)
        if text.strip():
            result["raw_text"] = text
            result["reason"] = "Gemini OCR teks saja (structured gagal)"
            return result

    # ── STEP 3: Google Vision fallback ──
    if USE_GOOGLE_VISION:
        text = ocr_google_vision(local_path)
        if text.strip():
            result["raw_text"] = text
            result["reason"] = "Google Vision OCR (structured gagal)"
            return result

    # ── STEP 4: Tesseract fallback ──
    text = ocr_tesseract(local_path)
    if text.strip():
        result["raw_text"] = text
        result["reason"] = "Tesseract OCR (structured gagal)"
        return result

    result["reason"] = "Gagal membaca teks dari gambar"
    return result
