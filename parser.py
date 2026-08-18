"""
Text Parser — Parse pesan WhatsApp jadi transaksi Flowku
Kategori sesuai schema Firestore: food, transport, shopping, entertainment,
bills, health, education, other

Format yang didukung:
  - "catat 50000 makan" / "catat 50rb makan"
  - "pengeluaran 50000 makan siang"
  - "pemasukan 500000 gaji"
  - "50rb makan" / "50000 transport grab"
"""
import re
import logging

logger = logging.getLogger(__name__)

# Mapping kata kunci ke kategori pengeluaran (sesuai schema Flowku)
CATEGORY_KEYWORDS = {
    "food": ["makan", "minum", "kopi", "teh", "jus", "nasi", "ayam", "mie", "sate", "bakso",
             "snack", "cemilan", "sarapan", "makan siang", "makan malam", "gofood", "grabfood",
             "resto", "kafe", "warung", "food", "drink", "coffee"],
    "transport": ["transport", "grab", "gojek", "ojek", "bensin", "parkir", "tol", "bus",
                  "kereta", "mrt", "taksi", "bbm", "motor", "mobil", "angkot", "transjakarta",
                  "pertamina", "shell", "vivo", "spbu", "solar", "pertalite", "pertamax",
                  "dexlite", "turbo", "ron", "premium", "bio solar"],
    "shopping": ["belanja", "shopping", "supermarket", "indomaret", "alfamart", "toko",
                 "market", "shopee", "tokopedia", "lazada", "bukalapak", "mall",
                 "circle k", "circle-k", "minimarket", "mart", "family mart", "lawson"],
    "health": ["kesehatan", "obat", "dokter", "rumah sakit", "apotek", "vitamin",
               "klinik", "medical", "checkup", "sakit", "therapi", "rawat", "bpjs"],
    "entertainment": ["hiburan", "nonton", "film", "game", "musik", "spotify", "netflix",
                      "youtube", "karaoke", "bioskop", "liburan", "jalan-jalan", "wisata", "rekreasi"],
    "bills": ["tagihan", "listrik", "air", "internet", "wifi", "pulsa", "cicilan",
              "sewa", "kos", "kontrakan", "pdam", "telepon", "asuransi", "token", "paket data"],
    "education": ["pendidikan", "buku", "kursus", "sekolah", "kuliah", "training",
                  "seminar", "workshop", "spp", "les", "akademik"],
    "beauty": ["kecantikan", "salon", "skincare", "kosmetik", "makeup", "facial", "creambath",
               "potong rambut", "pangkas", "beauty", "spa"],
    "home": ["rumah tangga", "dapur", "kasur", "lemari", "meja", "kursi", "alat dapur", "sabun",
             "shampoo", "detergen", "sapu", "pel", "renovasi"],
    "investment": ["investasi", "saham", "reksadana", "crypto", "kripto", "obligasi", "emas",
                   "reksa dana", "bibit", "bareksa", "invest"],
    "social": ["sosial", "hadiah", "kado", "donasi", "sedekah", "zakat", "kondangan", "sumbangan",
               "tumpengan", "gift", "donation"],
    "saving": ["tabungan", "saving", "celengan", "simpanan", "tabung", "goal"],
    "other_expense": ["lainnya", "other", "misc", "lain-lain"],
}

# Mapping kata kunci ke kategori pemasukan (sesuai schema Flowku)
INCOME_CATEGORY_KEYWORDS = {
    "salary": ["gaji", "salary", "gajian", "payroll", "upah"],
    "freelance": ["freelance", "proyek", "project", "sambilan", "desain", "coding", "nulis", "side hustle"],
    "business": ["bisnis", "usaha", "jualan", "dagang", "toko", "omset", "omzet", "warung", "untung", "profit", "sales", "penjualan"],
    "investment_in": ["dividen", "bunga", "kupon", "profit investasi", "capital gain", "hasil saham", "imbal hasil"],
    "bonus": ["bonus", "thr", "insentif", "hadiah", "giveaway", "tips", "reward"],
    "transfer": ["transfer masuk", "kiriman", "ditransfer", "dikirimi", "uang masuk", "transfer"],
    "other_income": ["lainnya", "other", "misc", "lain-lain", "pemasukan", "masuk", "income", "pendapatan"],
}

INCOME_KEYWORDS = [
    "gaji", "pemasukan", "masuk", "income", "hadiah", "transfer masuk",
    "cair", "untung", "bonus", "gajian", "pendapatan", "jualan", "profit",
    "freelance", "bisnis", "usaha", "salary", "dividen",
]


def parse_amount(text: str) -> int:
    """Parse jumlah uang dari text.
    Mendukung: 50000, 50rb, 50k, 50ribu, 3jt, 2.5jt, 1,5juta, 50.000, rp50000, rp 50.000.
    """
    text = text.lower().strip()
    text = re.sub(r"rp\.?\s*", "", text)

    def _parse_num(raw: str) -> float:
        """Convert string angka jadi float, support titik/koma sebagai desimal ATAU ribuan."""
        raw = raw.strip()
        # Jika ada titik/koma dengan tepat 1-2 digit di belakangnya → desimal
        # Contoh: "1.5", "2,5", "1.50"
        if re.search(r"[.,]\d{1,2}$", raw):
            normalized = raw.replace(",", ".")  # ganti koma desimal ke titik
            # Hapus titik ribuan yang ada (titik yang diikuti 3 digit lalu sesuatu lagi)
            normalized = re.sub(r"\.(\d{3})(?=[\d.])", r"\1", normalized)
            try:
                return float(normalized)
            except ValueError:
                pass
        # Selainnya: titik/koma sebagai separator ribuan
        cleaned = raw.replace(".", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    # Handle "rb" / "k" / "ribu" suffix → × 1.000
    match = re.match(r"([\d.,]+)\s*(rb|k|ribu)(?:\s|$)", text)
    if match:
        return int(_parse_num(match.group(1)) * 1_000)

    # Handle "jt" / "juta" suffix → × 1.000.000 (support desimal: 1.5jt, 2,5juta)
    match = re.match(r"([\d.,]+)\s*(jt|juta)(?:\s|$)", text)
    if match:
        return int(_parse_num(match.group(1)) * 1_000_000)

    # Plain number (titik/koma sebagai separator ribuan)
    cleaned = text.replace(".", "").replace(",", "")
    match = re.match(r"(\d+)", cleaned)
    if match:
        return int(match.group(1))

    return 0


def detect_category(text: str, tx_type: str = "expense", custom_categories: list = None) -> str:
    """Deteksi kategori dari text dengan prioritas kategori kustom lalu default."""
    text_lower = text.lower()

    # 1. Cek custom categories dari Firestore
    if custom_categories:
        for c in custom_categories:
            if c.get("type") == tx_type:
                label = c.get("label", "")
                if label and label.lower() in text_lower:
                    return c.get("id")

    # 2. Cek kategori statis bawaan
    if tx_type == "income":
        for category, keywords in INCOME_CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return category
        return "other_income"
    else:
        for category, keywords in CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return category
        return "other_expense"


def parse_catatan(message: str, custom_categories: list = None) -> dict | None:
    """
    Parse pesan jadi dict transaksi.
    Returns: {type, amount, category, description} atau None kalau gagal.
    type: "expense" atau "income"
    """
    msg = message.strip().lower()

    # Detect type
    tx_type = "expense"
    if any(w in msg for w in INCOME_KEYWORDS):
        tx_type = "income"

    # Hapus command keywords:
    # - Keyword umum: hapus di mana saja sebagai kata utuh
    # - "masuk": hanya hapus di awal kalimat (tidak boleh menghapus "transfer masuk")
    msg_clean = msg
    for word in ["catat", "catatan", "pengeluaran", "pemasukan", "keluar",
                 "income", "expense", "uang"]:
      msg_clean = re.sub(rf"\b{word}\b", "", msg_clean)
    # "masuk" hanya strip di awal kalimat
    msg_clean = re.sub(r"^\s*masuk\b", "", msg_clean)
    msg_clean = msg_clean.strip()

    # Extract amount — coba 4 pola:
    # Extract amount
    amount = 0
    description = ""

    # Suffix token: rb, k, ribu, jt, juta — harus diikuti spasi atau akhir string
    # (mencegah "k" dari "kopi" atau "jt" dari "jalan" ikut tertangkap)
    SUFFIX = r"(?:rb|k|ribu|jt|juta)(?=\s|$)"

    patterns = [
        # 1. RP prefix di depan angka (rp25000, rp 50rb)
        (rf"(rp\.?\s*[\d.,]+\s*(?:{SUFFIX})?)(\s+.*|$)", "amount_first"),
        # 2. Angka + suffix wajib di belakang (50rb, 15k, 3jt) — suffix tidak opsional
        (rf"([\d.,]+\s*{SUFFIX})(.*)", "amount_first"),
        # 3. Angka saja (plain, tanpa suffix)
        (r"([\d.,]+)(.*)", "amount_first"),
        # 4. Deskripsi di depan, angka di belakang (e.g. "tagihan wifi 300rb", "transfer masuk 200rb dari mama")
        (rf"^(.+?)\s+([\d.,]+\s*(?:{SUFFIX})?)\s*(.*)$", "desc_first"),
    ]

    for pattern, mode in patterns:
        match = re.match(pattern, msg_clean, re.IGNORECASE)
        if match:
            if mode == "desc_first":
                description = match.group(1).strip()
                amount = parse_amount(match.group(2).strip())
                extra = match.group(3).strip()
                if extra:
                    description = f"{description} {extra}".strip()
            else:
                amount = parse_amount(match.group(1).strip())
                description = match.group(2).strip() if match.group(2) else ""
            if amount > 0:
                break

    if amount <= 0:
        return None

    # Detect category
    category = detect_category(description if description else msg, tx_type=tx_type, custom_categories=custom_categories)

    return {
        "type": tx_type,
        "amount": amount,
        "category": category,
        "description": description.capitalize() if description else "",
    }


def parse_ocr_items(text: str, custom_categories: list = None) -> list:
    """
    Parse teks OCR struk jadi list item pengeluaran.
    Mendukung berbagai format struk Indonesia.
    """
    items = []
    lines = text.strip().split("\n")

    # Skip words — headers, footers, payment info
    skip_words = [
        "total", "subtotal", "sub total", "pajak", "tax", "ppn",
        "cash", "kembali", "change", "kartu", "debit", "qris",
        "bayar", "payment", "tunai", "non tunai", "e-money",
        "struk", "receipt", "nota", "invoice", "faktur",
        "kasir", "cashier", "operator", "no.", "nomor",
        "terima kasih", "thank you", "kembali lagi",
        "member",
        "grand total", "jumlah", "qty", "harga", "price",
        "tanggal", "date", "waktu", "time", "jam",
    ]

    # Common item patterns
    # Pattern 1: "ITEM NAME    25.000" or "ITEM NAME    Rp25.000"
    # Pattern 2: "2 x ITEM NAME    70.000" (qty x price)
    # Pattern 3: "ITEM NAME    2    12.500    25.000" (name qty unit_price total)

    # ── DISCOUNT DETECTION ──
    discount_keywords = ["diskon", "discount", "voucher", "potongan", "promo",
                         "cashback", "refund", "rebate", "potongan harga", "pengurangan",
                         "dp", "diskon member", "disc member", "member discount"]
    discount_items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        line_lower = line.lower()
        i += 1
        if not line:
            continue
        if not any(w in line_lower for w in discount_keywords):
            continue

        # Extract numbers from the line
        nums = re.findall(r"-?[\d.,]+", line)
        amounts = []
        for n in nums:
            cleaned = n.replace(".", "").replace(",", "")
            try:
                val = int(cleaned)
                if 100 <= abs(val) < 100000000:
                    amounts.append(abs(val))
            except ValueError:
                continue

        # Check for percentage pattern (e.g. "Diskon 10%")
        pct_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)

        nama = re.sub(r"[\d.,%\-]+", "", line).strip(" -·*.:;x×@#")
        nama = re.sub(r"\s+", " ", nama).strip()
        if not nama:
            nama = "Diskon"

        if amounts:
            # Use the LAST amount on the line as the discount value
            val = amounts[-1]
            # If line has minus sign OR keyword implies reduction → negative
            if "-" in line or any(w in line_lower for w in ["diskon", "discount", "voucher", "potongan", "promo", "cashback", "refund", "rebate", "pengurangan"]):
                val = -abs(val)
            discount_items.append({"nama": nama, "harga": val})
        elif pct_match:
            # Percentage only — look at next line for the amount
            pct = float(pct_match.group(1).replace(",", "."))
            next_amount = None
            if i < len(lines):
                next_line = lines[i].strip()
                next_nums = re.findall(r"-?[\d.,]+", next_line)
                for n in next_nums:
                    cleaned = n.replace(".", "").replace(",", "")
                    try:
                        v = int(cleaned)
                        if 100 <= v < 100000000:
                            next_amount = v
                            break
                    except ValueError:
                        continue
            if next_amount:
                i += 1  # consume the next line
                discount_items.append({"nama": nama, "harga": -abs(next_amount)})
            else:
                # No amount found — skip (can't compute without subtotal)
                logger.info(f"Discount percentage without amount: {line}")

    # ── ITEM PARSING ──
    for line in lines:
        line = line.strip()
        if not line:
            continue

        line_lower = line.lower()

        # Skip non-item lines
        if any(w in line_lower for w in skip_words):
            continue

        # Skip discount lines (already captured above)
        if any(w in line_lower for w in discount_keywords):
            continue

        # Skip lines that are just numbers (like receipt number)
        if re.match(r"^[\d\s\-./:]+$", line):
            continue

        # Skip very short lines (likely noise)
        if len(line) < 3:
            continue

        # Extract all numbers from the line
        numbers = re.findall(r"[\d.,]+", line)
        if not numbers:
            continue

        # Try to find the price — usually the last or second-to-last number
        # Filter out very small numbers (likely quantity) and very large (likely receipt IDs)
        price_candidates = []
        for num_str in numbers:
            cleaned = num_str.replace(".", "").replace(",", "")
            try:
                val = int(cleaned)
                if 500 <= val < 100000000:  # Rp500 to Rp99.999.999 (filter noise < 500)
                    price_candidates.append((num_str, val))
            except ValueError:
                continue

        if not price_candidates:
            continue

        # Use the last valid price candidate (usually the total for that line)
        price_str, price = price_candidates[-1]

        # Extract item name: remove all numbers and clean up
        name_part = line
        for num in numbers:
            name_part = name_part.replace(num, "")
        # Clean up common separators and OCR artifacts
        name_part = re.sub(r"[x×]\s*$", "", name_part)  # Remove trailing "x" (qty marker)
        name_part = name_part.strip(" -·*.,:;x×@#")
        # Collapse multiple spaces
        name_part = re.sub(r"\s+", " ", name_part).strip()

        if name_part and len(name_part) >= 2:
            items.append({
                "nama": name_part,
                "harga": price,
                "kategori": detect_category(name_part, tx_type="expense", custom_categories=custom_categories),
            })

    # Add discount items (with category)
    for d in discount_items:
        d["kategori"] = "other_expense"
        items.append(d)

    # Deduplicate items with same name (OCR sometimes reads same line twice)
    seen = set()
    unique_items = []
    for item in items:
        key = (item["nama"].lower(), item["harga"])
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    return unique_items


def format_rupiah(amount: int) -> str:
    """Format angka jadi Rupiah (support negatif)."""
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1000000:
        val = f"Rp{amount/1000000:,.1f}jt".replace(",", ".")
    elif amount >= 1000:
        val = f"Rp{amount:,.0f}".replace(",", ".")
    else:
        val = f"Rp{amount}"
    return f"{sign}{val}"
