"""
Flowku WhatsApp Chatbot — Main FastAPI App
Menerima webhook dari WAHA, proses pesan, simpan ke Firestore.
Schema sesuai BACKEND_MIGRATION_GUIDE.md
"""
import logging
import json
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import pytz

CATEGORY_EMOJIS = {
    "food": "🍔", "transport": "🚗", "shopping": "🛍️", "health": "💊",
    "entertainment": "🎮", "bills": "⚡", "education": "📚", "beauty": "💄",
    "home": "🏠", "investment": "📈", "social": "🎁", "saving": "🎯",
    "other_expense": "📦", "salary": "💰", "freelance": "💻", "business": "🏪",
    "investment_in": "📈", "bonus": "🎉", "transfer": "💸", "other_income": "✨",
    "refund": "💸",
}

from config import (
    APP_PORT, WEBHOOK_SECRET, OWNER_PHONE, REMINDER_HOUR_1, REMINDER_HOUR_2,
    SENDER_MODE, WABA_VERIFY_TOKEN,
)
from parser import parse_catatan, parse_ocr_items, format_rupiah
from firestore_db import (
    catat_transaksi, hitung_total_hari_ini, hitung_total_bulan_ini,
    save_ocr_result, get_budget_status, get_user_by_phone, verify_whatsapp,
    save_pending_transaction, set_ocr_cancelled, is_ocr_cancelled,
)
from waha import send_text as waha_send_text
from waha import send_image as waha_send_image
import waba
from ocr import extract_text_from_image, extract_items_from_image, extract_transfer_from_image
from reminder import cek_dan_kirim_reminder, cek_langganan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WIB = pytz.timezone("Asia/Jakarta")

# Scheduler for reminders
scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")

# ── Deduplication: track processed message IDs (auto-cleanup after 5 min) ──
import time as _time
_processed_msg_ids: dict[str, float] = {}  # msg_id -> timestamp
_DEDUP_TTL = 300  # 5 minutes

def _is_duplicate(msg_id: str) -> bool:
    """Check if msg_id was already processed. Returns True if duplicate."""
    if not msg_id:
        return False
    now = _time.time()
    # Cleanup expired entries
    expired = [k for k, t in _processed_msg_ids.items() if now - t > _DEDUP_TTL]
    for k in expired:
        del _processed_msg_ids[k]
    if msg_id in _processed_msg_ids:
        return True
    _processed_msg_ids[msg_id] = now
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown events."""
    scheduler.add_job(
        cek_dan_kirim_reminder, "cron",
        hour=REMINDER_HOUR_1, minute=0,
        id="reminder_siang", replace_existing=True,
    )
    scheduler.add_job(
        cek_dan_kirim_reminder, "cron",
        hour=REMINDER_HOUR_2, minute=0,
        id="reminder_malam", replace_existing=True,
    )
    scheduler.add_job(
        cek_langganan, "cron",
        hour=8, minute=0,
        id="cek_langganan", replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — reminders at {REMINDER_HOUR_1}:00 & {REMINDER_HOUR_2}:00 WIB")

    yield

    scheduler.shutdown()


app = FastAPI(title="Flowku Chatbot", lifespan=lifespan)


# ─────────────────────────────────────────────
# UNIFIED SENDER (WABA or WAHA)
# ─────────────────────────────────────────────

async def send_text(phone: str, text: str) -> bool:
    """Kirim pesan teks via sender aktif (WABA atau WAHA)."""
    if SENDER_MODE == "waba":
        return await waba.send_text(phone, text)
    return await waha_send_text(phone, text)


async def send_image(phone: str, image_url: str, caption: str = "") -> bool:
    """Kirim gambar via sender aktif (WABA atau WAHA)."""
    if SENDER_MODE == "waba":
        return await waba.send_image(phone, image_url, caption=caption)
    return await waha_send_image(phone, image_url, caption=caption)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def format_ocr_preview(items: list, ocr_label: str = "🤖 AI") -> str:
    """Format preview OCR items sebelum konfirmasi."""
    total = sum(item["harga"] for item in items)
    discount_total = sum(item["harga"] for item in items if item["harga"] < 0)
    discount_count = sum(1 for item in items if item["harga"] < 0)
    item_count = len(items) - discount_count
    if discount_count:
        msg = f"{ocr_label} Struk terbaca! {item_count} item + {discount_count} diskon:\n\n"
    else:
        msg = f"{ocr_label} Struk terbaca! {len(items)} item ditemukan:\n\n"
    for i, item in enumerate(items, 1):
        harga = item["harga"]
        if harga < 0:
            msg += f"  {i}. 💸 {item['nama']}: {format_rupiah(harga)}\n"
        else:
            emoji = CATEGORY_EMOJIS.get(item["kategori"], "•")
            msg += f"  {i}. {emoji} {item['nama']}: {format_rupiah(harga)}\n"
    msg += f"\n💸 Total: {format_rupiah(total)}\n"
    if discount_count:
        msg += f"  (hemat {format_rupiah(discount_total)})\n"
    msg += f"\n💾 Simpan semua item ke catatan?"
    msg += f"\n• Balas *Ya* / *Ok* untuk simpan"
    msg += f"\n• Balas *Batal* untuk batal"
    msg += f"\n• *hapus 1,3* — hapus item no 1 & 3"
    msg += f"\n• *edit 1 5000 makan* — ubah item no 1"
    return msg


def format_catatan_msg(saved: dict, catatan: dict, phone: str, uid: str = None) -> str:
    """Format pesan konfirmasi setelah catat."""
    emoji = "💸" if catatan["type"] == "expense" else "💰"
    tipe_label = "Pengeluaran" if catatan["type"] == "expense" else "Pemasukan"

    msg = (
        f"✅ {tipe_label} tercatat!\n\n"
        f"{emoji} *{format_rupiah(catatan['amount'])}*\n"
        f"📂 Kategori: {catatan['category'].replace('_', ' ').capitalize()}\n"
    )
    if catatan.get("description"):
        msg += f"📝 {catatan['description']}\n"

    # Ringkasan hari ini
    total = hitung_total_hari_ini(phone, uid=uid)
    msg += (
        f"\n📊 Hari ini:\n"
        f"  💸 Keluar: {format_rupiah(total['pengeluaran'])}\n"
    )
    if total['pemasukan'] > 0:
        msg += f"  💰 Masuk: {format_rupiah(total['pemasukan'])}\n"
    msg += f"  📝 {len(total['catatan'])} transaksi"

    # Budget warning (kalau ada)
    budget = get_budget_status(phone)
    if budget and catatan["category"] in budget:
        b = budget[catatan["category"]]
        if b["percentage"] >= 80:
            msg += f"\n\n⚠️ Anggaran {catatan['category'].capitalize()}: {b['percentage']}% terpakai!"

    return msg


def format_laporan(catatan: list, total_pengeluaran: int, total_pemasukan: int, label: str) -> str:
    """Format laporan ringkasan."""
    msg = f"📊 Laporan {label}\n\n"

    if not catatan:
        msg += "Belum ada transaksi."
        return msg

    # Group by category
    by_cat = {}
    for t in catatan:
        if t.get("type") == "expense":
            cat = t.get("category", "other_expense")
            by_cat[cat] = by_cat.get(cat, 0) + t.get("amount", 0)

    if by_cat:
        msg += "Pengeluaran per kategori:\n"
        for cat, jumlah in sorted(by_cat.items(), key=lambda x: -x[1]):
            emoji = CATEGORY_EMOJIS.get(cat, "•")
            msg += f"  {emoji} {cat.replace('_', ' ').capitalize()}: {format_rupiah(jumlah)}\n"

    msg += f"\n💸 Total Keluar: {format_rupiah(total_pengeluaran)}"
    if total_pemasukan > 0:
        msg += f"\n💰 Total Masuk: {format_rupiah(total_pemasukan)}"
        msg += f"\n📉 Selisih: {format_rupiah(total_pemasukan - total_pengeluaran)}"

    msg += f"\n📝 {len(catatan)} transaksi"
    return msg


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────

async def cmd_help() -> str:
    return (
        "💰 *Flowku Bot* — Asisten Keuangan Pribadimu\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📝 *CATAT PENGELUARAN*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Cukup ketik nominalnya, langsung tercatat!\n"
        "  • `50rb makan siang`\n"
        "  • `catat 25000 kopi`\n"
        "  • `tagihan wifi 300rb`\n"
        "  • `80rb skincare vitamin c`\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "💰 *CATAT PEMASUKAN*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "  • `pemasukan 3jt gaji bulanan`\n"
        "  • `masuk 500rb bonus`\n"
        "  • `1jt freelance desain logo`\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📊 *LAPORAN & CEK*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "  • *hari ini* — ringkasan transaksi hari ini\n"
        "  • *bulan ini* — ringkasan & saldo bulanan\n"
        "  • *anggaran* — cek sisa budget per kategori\n"
        "  • *kategori* — lihat semua kategori\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📸 *SCAN STRUK*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Kirim *foto struk* langsung ke chat ini,\n"
        "Flowku akan baca & catat otomatis!\n\n"

        "💡 *Tips:*\n"
        "Nominal bisa pakai: `rb`, `k`, `ribu`, `jt`, `juta`\n"
        "Contoh: `50rb` = `50k` = `50000`\n"
        "Ketik *bantuan* kapan saja untuk tampilkan menu ini."
    )


async def cmd_kategori() -> str:
    msg = "📂 *Kategori Default Flowku*\n\n"
    msg += "*💸 PENGELUARAN:*\n"
    expense_cats = [
        ("food", "🍔", "Makan & Minum"),
        ("transport", "🚗", "Transportasi"),
        ("shopping", "🛍️", "Belanja"),
        ("health", "💊", "Kesehatan"),
        ("entertainment", "🎮", "Hiburan"),
        ("bills", "⚡", "Tagihan & Utilitas"),
        ("education", "📚", "Pendidikan"),
        ("beauty", "💄", "Kecantikan"),
        ("home", "🏠", "Rumah Tangga"),
        ("investment", "📈", "Investasi"),
        ("social", "🎁", "Sosial & Hadiah"),
        ("saving", "🎯", "Tabungan Goal"),
        ("other_expense", "📦", "Lainnya"),
    ]
    for cat, emoji, label in expense_cats:
        msg += f"  {emoji} {label} (`{cat}`)\n"
        
    msg += "\n*💰 PEMASUKAN:*\n"
    income_cats = [
        ("salary", "💰", "Gaji"),
        ("freelance", "💻", "Freelance"),
        ("business", "🏪", "Bisnis"),
        ("investment_in", "📈", "Hasil Investasi"),
        ("bonus", "🎉", "Bonus"),
        ("transfer", "💸", "Transfer Masuk"),
        ("other_income", "✨", "Lainnya"),
    ]
    for cat, emoji, label in income_cats:
        msg += f"  {emoji} {label} (`{cat}`)\n"
        
    msg += "\nContoh: *catat 50000 makan*"
    return msg


async def cmd_hari_ini(phone: str) -> str:
    total = hitung_total_hari_ini(phone)
    return format_laporan(total["catatan"], total["pengeluaran"], total["pemasukan"], "Hari Ini")


async def cmd_bulan_ini(phone: str) -> str:
    total = hitung_total_bulan_ini(phone)
    return format_laporan(total["catatan"], total["pengeluaran"], total["pemasukan"], "Bulan Ini")


async def cmd_anggaran(phone: str) -> str:
    budget = get_budget_status(phone)
    if not budget:
        return "Belum ada anggaran yang diset. Set di aplikasi Flowku dulu ya."

    msg = "📊 Status Anggaran Bulan Ini\n\n"

    for cat, info in budget.items():
        emoji = CATEGORY_EMOJIS.get(cat, "•")
        pct = info["percentage"]
        status = "✅" if pct < 80 else "⚠️" if pct < 100 else "🚨"
        msg += (
            f"{status} {emoji} {cat.replace('_', ' ').capitalize()}\n"
            f"   {format_rupiah(info['spent'])} / {format_rupiah(info['limit'])} ({pct}%)\n\n"
        )

    return msg


# ─────────────────────────────────────────────
# SUSPICIOUS TRANSACTION DETECTOR
# ─────────────────────────────────────────────

import re as _re

# Suffix-like karakter yang sering jadi typo (bukan rb/k/ribu/jt/juta)
# Hanya tangkap suffix yang LANGSUNG menempel atau 1 spasi setelah angka,
# dan suffix tersebut berdiri sendiri (tidak diikuti huruf lain = bukan bagian kata)
_TYPO_SUFFIX_PATTERN = _re.compile(
    r"(?<!\w)(\d+)\s{0,1}([a-z]{1,3})(?!\w)",
    _re.IGNORECASE,
)
_VALID_SUFFIXES = {"rb", "k", "ribu", "jt", "juta", "rp"}


def is_suspicious_transaction(raw_text: str, catatan: dict) -> tuple[bool, str]:
    """
    Deteksi apakah transaksi terlihat mencurigakan / typo.
    Returns: (is_suspicious: bool, reason: str)
    """
    amount = catatan.get("amount", 0)
    raw_lower = raw_text.strip().lower()

    # 1. Nominal terlalu kecil (< Rp500) — kemungkinan lupa suffix
    if 0 < amount < 500:
        return True, f"Nominal {format_rupiah(amount)} sangat kecil, mungkin ada typo? (contoh: '10rb' bukan '10 m')"

    # 2. Nominal sangat besar (> Rp 100.000.000)
    if amount > 100_000_000:
        return True, f"Nominal {format_rupiah(amount)} sangat besar, pastikan sudah benar"

    # 3. Ada suffix tidak dikenal langsung setelah angka (kemungkinan typo suffix)
    # mis. "10 m makan" → suffix 'm' tidak valid
    # mis. "50rb makan" → suffix 'rb' valid, skip
    # mis. "10 makan" → 'makan' adalah kata panjang, tidak tertangkap regex ini
    for match in _TYPO_SUFFIX_PATTERN.finditer(raw_lower):
        suffix = match.group(2).lower()
        if suffix not in _VALID_SUFFIXES:
            return True, f"Suffix '*{suffix}*' tidak dikenal setelah angka {match.group(1)}, mungkin typo? (gunakan: rb, k, jt, juta)"

    return False, ""


# ─────────────────────────────────────────────
# MESSAGE HANDLER
# ─────────────────────────────────────────────

async def handle_text_message(phone: str, text: str) -> str:
    """Proses pesan teks dan return balasan."""
    msg = text.strip().lower()

    # 1. Lookup user from Firestore
    user = get_user_by_phone(phone)
    if not user:
        return (
            "⚠️ *Nomor WhatsApp Belum Terdaftar*\n\n"
            "Nomor Anda belum terdaftar di sistem Flowku.\n"
            "Silakan daftar/masuk ke aplikasi Flowku dan simpan nomor WhatsApp Anda di halaman Profil."
        )

    # 2. Check if verification command is sent
    if msg == "mulai flowku":
        success = verify_whatsapp(phone)
        if success:
            return (
                "🎉 *WhatsApp Berhasil Diverifikasi!*\n\n"
                "Selamat! WhatsApp Bot Flowku Anda telah aktif. Sekarang Anda dapat mulai mencatat keuangan langsung dari chat ini.\n\n"
                "Coba ketik: *catat 50rb makan siang*"
            )
        else:
            return "❌ Gagal melakukan verifikasi. Silakan coba lagi nanti."

    # 3. Enforce verification check
    if not user.get("waVerified", False):
        return (
            "⚠️ *Verifikasi Diperlukan*\n\n"
            "Nomor WhatsApp Anda sudah disimpan di Profil, tetapi belum diaktifkan.\n\n"
            "Silakan kirim pesan *Mulai Flowku* (tanpa tanda kutip) ke chat ini untuk mengaktifkan bot."
        )

    # 3b. Check for pending confirmation flow
    pending = user.get("pendingTransaction")
    if pending:
        # ── OCR: HAPUS ITEMS ──
        if pending.get("type") == "ocr_items" and msg.startswith("hapus"):
            nums_str = msg.replace("hapus", "").strip()
            try:
                indices = [int(n.strip()) - 1 for n in nums_str.split(",") if n.strip().isdigit()]
                items = pending["items"]
                removed = [items[i] for i in indices if 0 <= i < len(items)]
                if not removed:
                    return "❌ Nomor item tidak valid. Contoh: *hapus 1,3*"
                # Remove items (reverse order to keep indices valid)
                for i in sorted(indices, reverse=True):
                    if 0 <= i < len(items):
                        items.pop(i)
                if not items:
                    save_pending_transaction(phone, None)
                    return "❌ Semua item dihapus. Tidak ada yang tersimpan.\nKirim foto struk baru atau catat manual."
                # Update pending & show preview
                pending["items"] = items
                save_pending_transaction(phone, pending)
                nama_removed = ", ".join(r["nama"] for r in removed)
                return f"🗑️ Dihapus: {nama_removed}\n\n" + format_ocr_preview(items)
            except (ValueError, IndexError):
                return "❌ Format salah. Contoh: *hapus 1,3*"

        # ── OCR: EDIT ITEM ──
        if pending.get("type") == "ocr_items" and msg.startswith("edit"):
            parts = msg.split(None, 3)  # ["edit", "1", "5000", "makan siang"]
            if len(parts) < 3:
                return "❌ Format: *edit [no] [harga] [nama]*\nContoh: *edit 1 5000 makan siang*"
            try:
                idx = int(parts[1]) - 1
                items = pending["items"]
                if idx < 0 or idx >= len(items):
                    return f"❌ Item no {idx+1} tidak ada. Pilih 1-{len(items)}"
                new_harga = int(parts[2].replace(".", "").replace(",", ""))
                new_nama = parts[3].strip() if len(parts) > 3 else items[idx]["nama"]
                # Detect category from new name
                from parser import detect_category
                custom_categories = user.get("customCategories", [])
                new_kategori = detect_category(new_nama, tx_type="expense", custom_categories=custom_categories)
                old = items[idx].copy()
                items[idx] = {"nama": new_nama, "harga": new_harga, "kategori": new_kategori}
                save_pending_transaction(phone, pending)
                return (
                    f"✏️ Item {idx+1} diubah:\n"
                    f"  ❌ {old['nama']}: {format_rupiah(old['harga'])} ({old['kategori']})\n"
                    f"  ✅ {new_nama}: {format_rupiah(new_harga)} ({new_kategori})\n\n"
                    + format_ocr_preview(items)
                )
            except (ValueError, IndexError):
                return "❌ Format: *edit [no] [harga] [nama]*\nContoh: *edit 1 5000 makan siang*"

        if msg in ("ya", "y", "ok", "oke", "yes", "simpan"):
            # ── OCR BATCH ITEMS ──
            if pending.get("type") == "ocr_items":
                items = pending["items"]
                raw_text = pending.get("raw_text", "")

                # ── Separate positive items & discounts ──
                positive = [it for it in items if it["harga"] > 0]
                discounts = [it for it in items if it["harga"] < 0]
                total_discount = abs(sum(it["harga"] for it in discounts))
                discount_count = len(discounts)

                # Save positive items at original price
                total = 0
                saved_items = []
                for it in positive:
                    result = catat_transaksi(
                        user_phone=phone,
                        tipe="expense",
                        jumlah=it["harga"],
                        kategori=it["kategori"],
                        keterangan=it["nama"],
                        source="wa_bot_ocr",
                    )
                    if result:
                        total += it["harga"]
                        saved_items.append(it)

                # Save each discount as income (refund)
                saved_discounts = []
                for disc in discounts:
                    disc_amount = abs(disc["harga"])
                    result = catat_transaksi(
                        user_phone=phone,
                        tipe="income",
                        jumlah=disc_amount,
                        kategori="refund",
                        keterangan=disc["nama"],
                        source="wa_bot_ocr",
                    )
                    if result:
                        total -= disc_amount
                        saved_discounts.append(disc)

                all_saved = saved_items + saved_discounts
                save_ocr_result(phone, raw_text, all_saved)
                save_pending_transaction(phone, None)
                if all_saved:
                    uid = user.get("uid")
                    daily = hitung_total_hari_ini(phone, uid=uid)
                    item_total = discount_count + len(saved_items)
                    msg_out = f"✅ {item_total} item tersimpan!\n\n"
                    for item in saved_items:
                        emoji = CATEGORY_EMOJIS.get(item["kategori"], "•")
                        msg_out += f"  {emoji} {item['nama']}: {format_rupiah(item['harga'])} ({item['kategori']})\n"
                    for disc in saved_discounts:
                        emoji = CATEGORY_EMOJIS.get("refund", "•")
                        msg_out += f"  {emoji} {disc['nama']}: {format_rupiah(disc['harga'])} (refund)\n"
                    if discount_count:
                        msg_out += f"\n  💸 Hemat: {format_rupiah(total_discount)}"
                    msg_out += f"\n\n💸 Total: {format_rupiah(total)}"
                    msg_out += f"\n\n📊 Total hari ini: {format_rupiah(daily['pengeluaran'])}"
                    return msg_out
                else:
                    return "❌ Gagal menyimpan item. Silakan hubungi admin."

            # ── SINGLE TRANSACTION (existing) ──
            saved = catat_transaksi(
                user_phone=phone,
                tipe=pending["type"],
                jumlah=pending["amount"],
                kategori=pending["category"],
                keterangan=pending.get("description", ""),
            )
            save_pending_transaction(phone, None)
            if saved:
                uid = user.get("uid")
                return format_catatan_msg(saved, pending, phone, uid=uid)
            else:
                return "❌ Gagal menyimpan transaksi. Silakan hubungi admin."
        elif msg in ("batal", "b", "tidak", "no", "cancel", "t"):
            save_pending_transaction(phone, None)
            set_ocr_cancelled(phone, True)  # cancel in-flight OCR jika sedang jalan
            return "❌ *Pencatatan dibatalkan*\n\nTransaksi Anda tidak disimpan."
        else:
            # ── OCR pending: JANGAN auto-cancel, minta user pilih ──
            if pending.get("type") == "ocr_items":
                return (
                    "⏳ Masih ada struk yang belum disimpan.\n\n"
                    "Pilih dulu:\n"
                    "• *Ya* — simpan semua item\n"
                    "• *Batal* — buang semua\n"
                    "• *hapus [no]* — hapus item\n"
                    "• *edit [no] [harga] [nama]* — ubah item"
                )
            # Single transaction: auto-cancel seperti biasa
            save_pending_transaction(phone, None)

    # 4. If verified, continue to standard commands & parsing
    custom_categories = user.get("customCategories", [])

    if msg in ["help", "bantuan", "menu", "/start", "/help"]:
        return await cmd_help()

    if msg in ["kategori", "categories", "category"]:
        return await cmd_kategori()

    if msg in ["hari ini", "today", "laporan hari ini"]:
        return await cmd_hari_ini(phone)

    if msg in ["bulan ini", "this month", "laporan bulan ini"]:
        return await cmd_bulan_ini(phone)

    if msg in ["anggaran", "budget", "budget status"]:
        return await cmd_anggaran(phone)

    # Try parse as catatan
    catatan = parse_catatan(text, custom_categories=custom_categories)
    if catatan:
        # Check if transaction is ambiguous ("rancu") or suspicious (typo/nyeleneh)
        is_rancu = catatan["category"] in ("other_expense", "other_income") or not catatan.get("description")
        suspicious, suspicious_reason = is_suspicious_transaction(text, catatan)

        if suspicious:
            save_pending_transaction(phone, catatan)
            emoji = "💸" if catatan["type"] == "expense" else "💰"
            desc_label = catatan.get("description") or "-"
            return (
                "⚠️ *Konfirmasi Transaksi*\n\n"
                f"Ada yang perlu dikonfirmasi: _{suspicious_reason}_\n\n"
                f"{emoji} *{format_rupiah(catatan['amount'])}*\n"
                f"📂 Kategori: {catatan['category'].replace('_', ' ').capitalize()}\n"
                f"📝 Keterangan: {desc_label}\n\n"
                "Apakah ini benar?\n"
                "• Balas *Ya* / *Ok* untuk menyimpan\n"
                "• Balas *Batal* / *Tidak* untuk membatalkan"
            )

        if is_rancu:
            save_pending_transaction(phone, catatan)
            emoji = "💸" if catatan["type"] == "expense" else "💰"
            desc_label = catatan.get("description") or "-"
            return (
                "🔍 *Transaksi Kurang Detail / Kategori Lainnya*\n\n"
                "Kami mendeteksi pencatatan Anda kurang detail:\n"
                f"{emoji} *{format_rupiah(catatan['amount'])}* (Kategori: {catatan['category'].replace('_', ' ').capitalize()})\n"
                f"📝 Keterangan: {desc_label}\n\n"
                "Apakah Anda ingin menyimpan transaksi ini?\n"
                "• Balas *Ya* / *Ok* untuk menyimpan\n"
                "• Balas *Batal* / *Tidak* untuk membatalkan"
            )

        saved = catat_transaksi(
            user_phone=phone,
            tipe=catatan["type"],
            jumlah=catatan["amount"],
            kategori=catatan["category"],
            keterangan=catatan.get("description", ""),
        )
        if saved:
            uid = user.get("uid")
            return format_catatan_msg(saved, catatan, phone, uid=uid)
        else:
            return "❌ Gagal menyimpan transaksi. Silakan hubungi admin."

    # Unknown command
    return (
        "Hmm, gue ga ngerti pesannya 🤔\n\n"
        "Coba ketik *bantuan* untuk liat perintah yang tersedia.\n\n"
        "Contoh: *catat 25000 makan*"
    )


async def handle_image_message(phone: str, media_url: str, base64_data: str = None) -> str:
    """Proses gambar (foto struk) via OCR."""
    # 1. Lookup user from Firestore
    user = get_user_by_phone(phone)
    if not user:
        return (
            "⚠️ *Nomor WhatsApp Belum Terdaftar*\n\n"
            "Nomor Anda belum terdaftar di sistem Flowku.\n"
            "Silakan daftar/masuk ke aplikasi Flowku dan simpan nomor WhatsApp Anda di halaman Profil."
        )

    # 2. Enforce verification check
    if not user.get("waVerified", False):
        return (
            "⚠️ *Verifikasi Diperlukan*\n\n"
            "Nomor WhatsApp Anda belum diaktifkan.\n\n"
            "Silakan kirim pesan *Mulai Flowku* (tanpa tanda kutip) ke chat ini untuk mengaktifkan bot."
        )

    custom_categories = user.get("customCategories", [])

    if not media_url and not base64_data:
        return "Gagal terima gambar. Coba kirim ulang."

    # ── CLEAR ocrCancelled flag sebelum mulai OCR baru ──
    set_ocr_cancelled(phone, False)

    # ── CEK: ada struk pending belum disimpan? ──
    pending = user.get("pendingTransaction")
    if pending and pending.get("type") == "ocr_items":
        old_count = len(pending.get("items", []))
        old_total = sum(i["harga"] for i in pending.get("items", []))
        from parser import format_rupiah
        logger.info(f"Replacing pending OCR ({old_count} items, {format_rupiah(old_total)}) with new image")
        save_pending_transaction(phone, None)
        replace_warning = f"⚠️ Struk sebelumnya ({old_count} item, {format_rupiah(old_total)}) dibatalkan.\n\n"
    else:
        replace_warning = ""

    # ── PIPELINE: Pre-check (Tesseract) → Gemini structured OCR ──
    result = await extract_items_from_image(media_url, base64_data=base64_data)

    # Pre-check gagal → bukan struk
    if not result["is_receipt"]:
        reason = result["reason"]
        error_messages = {
            "GAMBAR_TIDAK_ADA_TEKS": (
                "❌ Foto ini sepertinya bukan struk/nota.\n\n"
                "Tidak terdeteksi teks pada gambar. Kemungkinan:\n"
                "• Foto gelap atau buram\n"
                "• Foto bukan struk (misal: selfie, pemandangan, screenshot chat)\n\n"
                "📋 *Kirim foto struk/nota yang valid:*\n"
                "• Struk belanja (Alfamart, Indomaret, supermarket)\n"
                "• Nota makan di restoran/warung\n"
                "• Struk SPBU (bensin)\n"
                "• Bukti transfer/QRIS\n"
                "• Invoice belanja online\n\n"
                "💡 *Tips foto yang bagus:*\n"
                "• Pastikan cahaya cukup terang\n"
                "• Foto dari atas, tegak lurus\n"
                "• Semua teks harus terbaca jelas"
            ),
            "BUKAN_STRUK": (
                "❌ Gambar ini bukan struk atau nota belanja.\n\n"
                "Flowku hanya bisa membaca foto struk/nota/bukti transaksi.\n\n"
                "📋 *Yang bisa dibaca:*\n"
                "• Struk minimarket (Alfamart, Indomaret, Circle K)\n"
                "• Nota restoran/warung/kafe\n"
                "• Struk SPBU (Pertamina, Shell, BP)\n"
                "• Bukti pembayaran QRIS/transfer\n"
                "• Invoice e-commerce (Shopee, Tokopedia, dll)\n\n"
                "Ketik *catat 25000 makan* untuk input manual."
            ),
            "GAMBAR_TIDAK_JELAS": (
                "❌ Foto kurang jelas, tidak bisa dibaca.\n\n"
                "💡 *Coba lagi dengan:*\n"
                "• Foto dari atas (bird's eye view)\n"
                "• Pastikan cahaya terang dan tidak ada bayangan\n"
                "• Jangan goyang saat foto\n"
                "• Semua tulisan harus terbaca\n\n"
                "Atau ketik *catat 25000 makan* untuk input manual."
            ),
        }
        msg = error_messages.get(reason, error_messages["GAMBAR_TIDAK_JELAS"])
        return msg

    items = result["items"]
    raw_text = ""
    gemini_tried = False

    if items:
        logger.info(f"Gemini structured OCR: {len(items)} items directly")
        gemini_tried = True
    else:
        # Gemini return None/empty → kemungkinan bukan struk
        gemini_tried = True
        # Fallback: Tesseract text + regex parsing
        raw_text = result.get("reason", "")
        if raw_text:
            logger.info(f"Tesseract fallback: {len(raw_text)} chars")
            items = parse_ocr_items(raw_text, custom_categories=custom_categories)

    if not items:
        # Kalau Gemini sudah coba dan return kosong → bukan struk
        if gemini_tried and not raw_text:
            return (
                "❌ Foto ini bukan struk atau nota belanja.\n\n"
                "Tidak ditemukan item belanja pada gambar.\n\n"
                "📋 *Kirim foto yang valid:*\n"
                "• Struk minimarket (Alfamart, Indomaret)\n"
                "• Nota restoran/warung\n"
                "• Struk SPBU (bensin)\n"
                "• Bukti pembayaran QRIS\n\n"
                "Atau ketik *catat 25000 makan* untuk input manual."
            )
        return (
            "❌ Struk terbaca tapi tidak ditemukan item yang jelas.\n\n"
            "Kemungkinan:\n"
            "• Struk kosong atau tidak lengkap\n"
            "• Format struk tidak umum\n"
            "• Foto terpotong atau terlalu buram\n\n"
            "📋 *Coba:*\n"
            "• Foto ulang dengan lebih jelas\n"
            "• Atau ketik manual: *catat 25000 makan siang*"
        )

    # ── KONFIRMASI DULU SEBELUM SIMPAN ──
    ocr_label = "🤖 AI" if not raw_text else "📸"

    # ── CANCEL CHECK: cek apakah user sudah cancel selama OCR berjalan ──
    if is_ocr_cancelled(phone):
        logger.info(f"OCR completed but user cancelled during processing — skipped for {phone}")
        set_ocr_cancelled(phone, False)  # reset flag
        return "✅ Pencatatan sudah dibatalkan."

    # Simpan ke pending (belum simpan ke transaksi)
    save_pending_transaction(phone, {
        "type": "ocr_items",
        "items": items,
        "raw_text": raw_text or f"[Gemini structured OCR: {len(items)} items]",
    })

    return replace_warning + format_ocr_preview(items, ocr_label)


# ─────────────────────────────────────────────
# WEBHOOK ENDPOINT
# ─────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    """Terima webhook dari WAHA."""
    # Validasi webhook secret header
    secret = request.headers.get("x-webhook-secret")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = body.get("event", "")
    payload = body.get("payload", {})
    session = body.get("session", "")

    logger.info(f"Webhook: event={event}, session={session}")

    if session != "default":
        return {"status": "ignored", "reason": "wrong session"}

    if event == "message":
        await handle_incoming_message(payload)

    return {"status": "ok"}


async def _process_waha_image(phone: str, media_url: str, base64_data=None):
    """Background task: OCR + kirim hasil via WAHA (tidak blokir webhook)."""
    try:
        await send_text(phone, "📸 Sedang membaca struk...")
        response = await handle_image_message(phone, media_url, base64_data=base64_data)
        await send_text(phone, response)
    except Exception as e:
        logger.error(f"WAHA background image error: {e}", exc_info=True)
        try:
            await send_text(phone, "Ada error, coba lagi ya 😅")
        except Exception:
            pass


async def handle_incoming_message(payload: dict):
    """Proses pesan masuk dari WAHA webhook."""
    if payload.get("fromMe", False):
        return

    # Extract phone - support both legacy (chatId) and GOWS (from/_data) formats
    chat_id = payload.get("chatId", "")
    if chat_id:
        phone = chat_id.replace("@c.us", "").replace("@g.us", "")
    else:
        # GOWS format: _data may be dict or JSON string
        
        _data = payload.get("_data", {})
        if isinstance(_data, str):
            try:
                _data = json.loads(_data)
            except Exception:
                _data = {}
        _info = _data.get("Info", {}) if isinstance(_data, dict) else {}
        sender_alt = _info.get("SenderAlt", "")
        if sender_alt:
            phone = sender_alt
            phone = phone.replace("@s.whatsapp.net", "").replace("@c.us", "")
            phone = phone.split(":")[0]  # strip device suffix
        else:
            # Try Chat field - might be phone@s.whatsapp.net
            chat_raw = _info.get("Chat", "")
            phone = chat_raw.replace("@s.whatsapp.net", "").replace("@c.us", "").replace("@g.us", "")
            if not phone or "@" in phone:
                # Last resort: try from field
                from_raw = payload.get("from", "")
                phone = from_raw.replace("@c.us", "").replace("@s.whatsapp.net", "")
                if "@" in phone:
                    phone = ""

    # Extract type - GOWS may not send type, detect from payload
    msg_type = payload.get("type", "")
    body = payload.get("body", "")
    has_media = payload.get("hasMedia", False)

    if not msg_type:
        if has_media or payload.get("media"):
            msg_type = "image"
        elif body:
            msg_type = "chat"

    logger.info(f"Incoming: type={msg_type}, from={phone}, body={body[:50]}")

    if not phone:
        logger.warning(f"Could not extract phone from payload")
        return

    try:
        if msg_type in ("text", "chat"):
            response = await handle_text_message(phone, body)
            await send_text(phone, response)

        elif msg_type == "image":
            media_url = payload.get("mediaUrl", "")
            base64_data = None

            if not media_url:
                media_raw = payload.get("media", "")
                # GOWS sends media as dict with url/mimetype/base64 fields
                if isinstance(media_raw, dict):
                    logger.info(f"GOWS media dict keys: {list(media_raw.keys())}")
                    media_url = media_raw.get("url", "") or media_raw.get("directDownloadURL", "")
                    if not media_url and media_raw.get("base64"):
                        base64_data = media_raw["base64"]
                        logger.info("Got base64 media from payload.media dict")
                    elif not media_url and media_raw.get("data"):
                        base64_data = media_raw["data"]
                        logger.info("Got base64 data from payload.media dict")
                elif isinstance(media_raw, str) and media_raw:
                    # String — could be URL or base64
                    if media_raw.startswith("http://") or media_raw.startswith("https://"):
                        media_url = media_raw
                    elif media_raw.startswith("data:") or len(media_raw) > 500:
                        base64_data = media_raw
                        logger.info("Got base64 media data string from payload.media")

            if not media_url and not base64_data:
                media_url = payload.get("_data", {}).get("mediaData", {}).get("mediaUrl", "")

            # Also check _data.Message for image message data
            if not media_url and not base64_data:
                _data = payload.get("_data", {})
                if isinstance(_data, str):
                    import json as _json
                    try:
                        _data = _json.loads(_data)
                    except Exception:
                        _data = {}
                msg_data = _data.get("Message", {})
                if isinstance(msg_data, dict):
                    img_msg = msg_data.get("imageMessage", {})
                    if isinstance(img_msg, dict):
                        # GOWS may have directDownloadURL or url
                        media_url = img_msg.get("directDownloadURL", "") or img_msg.get("url", "")
                        if not media_url and img_msg.get("mediaKey"):
                            logger.info("Image has mediaKey but no direct URL — may need WAHA media API")

            logger.info(f"Image processing: media_url={'yes' if media_url else 'no'}, base64={'yes' if base64_data else 'no'}")
            # ── Background task: return webhook cepat, OCR jalan di belakang ──
            asyncio.create_task(_process_waha_image(phone, media_url, base64_data))

        else:
            await send_text(phone, "Ketik *bantuan* untuk liat perintah yang tersedia.")

    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        await send_text(phone, "Ada error, coba lagi ya 😅")


# ─────────────────────────────────────────────
# WABA WEBHOOK (Meta Cloud API)
# ─────────────────────────────────────────────

@app.get("/waba/webhook")
async def waba_verify(request: Request):
    """Webhook verification endpoint (GET) — Meta Cloud API."""
    from fastapi.responses import PlainTextResponse
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    logger.info(f"WABA verify: mode={mode}, token={token}, challenge={challenge}")

    if mode == "subscribe" and token == WABA_VERIFY_TOKEN:
        logger.info("WABA webhook verified!")
        return PlainTextResponse(challenge)
    else:
        logger.warning("WABA verification failed!")
        return Response(status_code=403, content="Forbidden")


@app.post("/waba/webhook")
async def waba_webhook(request: Request):
    """Webhook callback endpoint (POST) — Meta Cloud API."""
    body = await request.json()

    logger.info(f"WABA webhook: {json.dumps(body, indent=2)[:1000]}")

    # Validate
    if body.get("object") != "whatsapp_business_account":
        logger.warning(f"WABA unexpected object: {body.get('object')}")
        return Response(status_code=400, content="Bad Request")

    # Process entries
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            field = change.get("field")

            if field == "messages":
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                for msg in messages:
                    await _handle_waba_message(msg, contacts)

            elif field == "account_update":
                statuses = value.get("statuses", [])
                for status in statuses:
                    logger.info(f"WABA status: {status.get('id')} -> {status.get('status')}")

    # Always return 200 quickly
    return {"status": "ok"}


async def _process_waba_image(sender: str, media_id: str, msg_id: str = ""):
    """Background task: download media + OCR + kirim hasil (tidak blokir webhook).
    
    KirimDev media download flow:
    1. GET /v1/{phone_id}/messages/{wamid}/media → returns download URL
    2. GET the download URL to get the actual bytes
    
    Fallback: GET /v1/{media_id} if KirimDev proxy also supports Meta compat.
    """
    try:
        # KirimDev: use wamid (msg_id) to fetch media URL via KirimDev endpoint
        # Falls back to media_id if wamid not available
        media_url = await waba.get_media_url(media_id, wamid=msg_id)
        base64_data = None
        if media_url:
            raw = await waba.download_media(media_url)
            if raw:
                import base64 as b64
                base64_data = b64.b64encode(raw).decode()
                logger.info(f"KirimDev image downloaded: {len(raw)} bytes (msg_id={msg_id})")
            else:
                logger.warning("KirimDev image download returned empty")
        else:
            logger.warning(f"KirimDev could not get media URL for {media_id} (wamid={msg_id})")

        await send_text(sender, "📸 Sedang membaca struk...")
        response = await handle_image_message(sender, media_url, base64_data=base64_data)
        await send_text(sender, response)
    except Exception as e:
        logger.error(f"KirimDev background image error: {e}", exc_info=True)
        try:
            await send_text(sender, "Ada error, coba lagi ya 😅")
        except Exception:
            pass


async def _handle_waba_message(msg: dict, contacts: list):
    """Proses satu pesan WABA — translate ke handler yang sama."""
    sender = msg.get("from", "")
    msg_type = msg.get("type", "")
    msg_id = msg.get("id", "")

    # ── DEDUP: Meta retry webhook kalau response lambat → jangan proses 2x ──
    if msg_id and _is_duplicate(msg_id):
        logger.info(f"WABA duplicate msg_id={msg_id} (retry) — skipped")
        return

    contact_name = "Unknown"
    for c in contacts:
        if c.get("wa_id") == sender:
            contact_name = c.get("profile", {}).get("name", "Unknown")
            break

    logger.info(f"WABA msg: from={contact_name}({sender}) type={msg_type} id={msg_id}")

    try:
        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")
            logger.info(f"WABA text: {text[:100]}")
            response = await handle_text_message(sender, text)
            await send_text(sender, response)

        elif msg_type == "image":
            media_id = msg.get("image", {}).get("id", "")
            logger.info(f"WABA image: media_id={media_id} msg_id={msg_id}")

            if media_id:
                # ── Background task: return 200 cepat, OCR jalan di belakang ──
                asyncio.create_task(_process_waba_image(sender, media_id, msg_id))
            else:
                await send_text(sender, "Gagal terima gambar. Coba kirim ulang.")

        elif msg_type == "button":
            button_text = msg.get("button", {}).get("text", "")
            logger.info(f"WABA button: {button_text}")
            response = await handle_text_message(sender, button_text)
            await send_text(sender, response)

        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            # Handle list replies and button replies
            if interactive.get("type") == "button_reply":
                btn_text = interactive.get("button_reply", {}).get("title", "")
                response = await handle_text_message(sender, btn_text)
                await send_text(sender, response)
            elif interactive.get("type") == "list_reply":
                list_text = interactive.get("list_reply", {}).get("title", "")
                response = await handle_text_message(sender, list_text)
                await send_text(sender, response)
            else:
                await send_text(sender, "Ketik *bantuan* untuk liat perintah yang tersedia.")

        else:
            logger.info(f"WABA unhandled type: {msg_type}")
            await send_text(sender, "Ketik *bantuan* untuk liat perintah yang tersedia.")

    except Exception as e:
        logger.error(f"WABA error handling message: {e}", exc_info=True)
        await send_text(sender, "Ada error, coba lagi ya 😅")


# ─────────────────────────────────────────────
# CHATIN INTEGRATION ENDPOINT
# ─────────────────────────────────────────────

@app.post("/chatin/process")
async def chatin_process(request: Request):
    """
    Terima pesan dari Chatin dashboard, proses via Flowku bot logic,
    dan return reply text (tanpa mengirim WhatsApp — Chatin yang kirim).
    """
    from config import CHATIN_WEBHOOK_SECRET

    secret = request.headers.get("x-chatin-secret", "")
    if not CHATIN_WEBHOOK_SECRET or secret != CHATIN_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Chatin secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    phone = body.get("phone", "")
    text = body.get("text", "")
    msg_type = body.get("type", "text")

    if not phone:
        return {"reply": "⚠️ Nomor telepon tidak ditemukan."}

    logger.info(f"[Chatin] Incoming: type={msg_type}, from={phone}, text={text[:60]}")

    try:
        if msg_type == "text" and text:
            reply = await handle_text_message(phone, text)
        elif msg_type == "image":
            media_url = body.get("media_url", "")
            if media_url:
                reply = await handle_image_message(phone, media_url)
            else:
                reply = "⚠️ URL gambar tidak ditemukan."
        else:
            reply = "Ketik *bantuan* untuk liat perintah yang tersedia."
    except Exception as e:
        logger.error(f"[Chatin] Error processing message: {e}", exc_info=True)
        reply = "Ada error saat memproses pesan, coba lagi ya 😅"

    return {"reply": reply}


# ─────────────────────────────────────────────
# HEALTH & INFO ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "Flowku WhatsApp Chatbot", "status": "running", "version": "1.0.0"}


@app.get("/health")
async def health():
    from config import SENDER_MODE
    session = {"status": "N/A"}

    # Check Firestore connection
    try:
        user = get_user_by_phone(OWNER_PHONE)
        firestore_ok = True
        user_found = user is not None
    except Exception:
        firestore_ok = False
        user_found = False

    return {
        "status": "ok",
        "sender_mode": SENDER_MODE,
        "waha_session": session.get("status", "N/A"),
        "firestore": "connected" if firestore_ok else "error",
        "user_registered": user_found,
        "owner_phone": OWNER_PHONE or "NOT SET",
        "reminders": f"{REMINDER_HOUR_1}:00 & {REMINDER_HOUR_2}:00 WIB",
    }


@app.post("/test/send")
async def test_send(phone: str, text: str, x_webhook_secret: str = Header(None)):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    result = await send_text(phone, text)
    return {"sent": result, "to": phone}


@app.post("/test/reminder")
async def test_reminder(x_webhook_secret: str = Header(None)):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    await cek_dan_kirim_reminder()
    return {"status": "reminder sent"}


# ─────────────────────────────────────────────
# SCAN RECEIPT API (Mobile App)
# ─────────────────────────────────────────────

@app.post("/api/scan-receipt")
async def scan_receipt(request: Request):
    """
    Scan bukti transfer atau struk belanja.
    Mobile app kirim gambar (base64), server proses OCR, return items/transfer data.

    Body:
      type: "transfer" | "receipt"
      image: base64 encoded image
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    scan_type = body.get("type", "receipt")
    image_data = body.get("image", "")

    if not image_data:
        raise HTTPException(status_code=400, detail="Missing 'image' field (base64)")

    # Strip data URL prefix if present (e.g. "data:image/jpeg;base64,...")
    if image_data.startswith("data:"):
        image_data = image_data.split(",", 1)[1] if "," in image_data else image_data

    logger.info(f"Scan receipt: type={scan_type}, image_len={len(image_data)}")

    try:
        if scan_type == "transfer":
            result = await extract_transfer_from_image(base64_data=image_data)

            if result["success"] and result["data"]:
                return {
                    "type": "transfer",
                    "data": result["data"],
                }
            else:
                # Fallback: return raw text for client-side parsing
                return {
                    "type": "transfer",
                    "data": None,
                    "rawText": result.get("raw_text", ""),
                    "reason": result.get("reason", "Gagal membaca bukti transfer"),
                }

        elif scan_type == "receipt":
            result = await extract_items_from_image("", base64_data=image_data)

            if result["is_receipt"] and result["items"]:
                total = sum(item.get("harga", 0) for item in result["items"])
                return {
                    "type": "receipt",
                    "data": {
                        "items": result["items"],
                        "total": total,
                    },
                }
            elif result["is_receipt"]:
                # Items parsed from text fallback
                return {
                    "type": "receipt",
                    "data": None,
                    "rawText": result.get("reason", ""),
                    "reason": "Struk terbaca tapi gagal parse items",
                }
            else:
                return {
                    "type": "receipt",
                    "data": None,
                    "reason": result.get("reason", "Gambar bukan struk"),
                }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid type '{scan_type}'. Use 'transfer' or 'receipt'."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Scan receipt error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"OCR processing error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Flowku Chatbot on port {APP_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
