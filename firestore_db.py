"""
Firestore Service — CRUD transaksi Flowku
Schema sesuai BACKEND_MIGRATION_GUIDE.md

Collections:
  - users: profil, isPremium, waPhone
  - couples: mode relasi (solo/couple)
  - transactions: data keuangan utama
  - budgets: limit anggaran per kategori
"""
from google.cloud import firestore
from datetime import datetime
import pytz
import logging

from config import FIRESTORE_PROJECT_ID, GOOGLE_APPLICATION_CREDENTIALS

logger = logging.getLogger(__name__)
WIB = pytz.timezone("Asia/Jakarta")


_db = None


def get_db():
    """Get Firestore client with service account cached as singleton."""
    global _db
    if _db is None:
        _db = firestore.Client.from_service_account_json(
            GOOGLE_APPLICATION_CREDENTIALS,
            project=FIRESTORE_PROJECT_ID,
        )
    return _db


def get_user_by_phone(phone: str) -> dict | None:
    """
    Cari user berdasarkan waPhone.
    Returns: {uid, coupleId, isPremium, ...} atau None
    """
    db = get_db()
    docs = db.collection("users").where("waPhone", "==", phone).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        data["uid"] = doc.id
        return data
    return None


def verify_whatsapp(phone: str) -> bool:
    """
    Set waVerified = True untuk user dengan waPhone tersebut.
    """
    db = get_db()
    docs = db.collection("users").where("waPhone", "==", phone).limit(1).stream()
    for doc in docs:
        doc.reference.update({
            "waVerified": True,
            "waVerifiedAt": firestore.SERVER_TIMESTAMP
        })
        return True
    return False


def save_pending_transaction(phone: str, pending_tx: dict | None) -> bool:
    """
    Simpan transaksi pending ke profil user, atau hapus jika None.
    """
    db = get_db()
    docs = db.collection("users").where("waPhone", "==", phone).limit(1).stream()
    for doc in docs:
        doc.reference.update({"pendingTransaction": pending_tx})
        return True
    return False


def set_ocr_cancelled(phone: str, cancelled: bool) -> bool:
    """
    Set/clear ocrCancelled flag on user doc.
    Used to cancel in-flight OCR when user types 'batal'.
    """
    db = get_db()
    docs = db.collection("users").where("waPhone", "==", phone).limit(1).stream()
    for doc in docs:
        doc.reference.update({"ocrCancelled": cancelled})
        return True
    return False


def is_ocr_cancelled(phone: str) -> bool:
    """
    Check if ocrCancelled flag is set for this user.
    """
    db = get_db()
    docs = db.collection("users").where("waPhone", "==", phone).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        return data.get("ocrCancelled", False)
    return False


def get_couple(uid: str) -> dict | None:
    """
    Ambil data couple berdasarkan uid.
    Returns: {coupleId, mode, status, uids, ...} atau None
    """
    db = get_db()
    docs = db.collection("couples").where("uids", "array_contains", uid).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        data["coupleId"] = doc.id
        return data
    return None


def catat_transaksi(user_phone: str, tipe: str, jumlah: int, kategori: str,
                    keterangan: str = "", source: str = "wa_bot") -> dict | None:
    """
    Simpan transaksi ke collection 'transactions'.
    Schema sesuai addTransaction & whatsappWebhook yang ada.

    Args:
        user_phone: nomor WA user (untuk lookup uid & coupleId)
        tipe: "expense" atau "income"
        jumlah: nominal
        kategori: food, transport, other, dll
        keterangan: deskripsi transaksi
        source: "wa_bot" (default) atau "app"
    """
    db = get_db()

    # Lookup user
    user = get_user_by_phone(user_phone)
    if not user:
        logger.warning(f"User not found for phone {user_phone}")
        # Fallback: simpan dengan uid = phone (belum terdaftar)
        uid = user_phone
        couple_id = f"solo_{user_phone}"
        uids = [user_phone]
    else:
        uid = user["uid"]
        couple = get_couple(uid)
        if couple:
            couple_id = couple["coupleId"]
            uids = couple.get("uids", [uid])
        else:
            couple_id = f"solo_{uid}"
            uids = [uid]

    # Build transaction document (sesuai schema yang ada)
    tx_ref = db.collection("transactions").document()
    now = datetime.now(WIB)

    new_tx = {
        "txId": tx_ref.id,
        "coupleId": couple_id,
        "uid": uid,
        "userId": uid,  # backward compatibility
        "uids": uids,
        "type": tipe,  # "expense" atau "income"
        "amount": jumlah,
        "category": kategori,
        "description": keterangan,
        "notes": "Dicatat otomatis via WhatsApp Bot",
        "who": uid,
        "date": firestore.SERVER_TIMESTAMP,
        "source": source,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }

    tx_ref.set(new_tx)
    logger.info(f"Transaction saved: {tipe} {jumlah} ({kategori}) for {user_phone}")

    return {
        "txId": tx_ref.id,
        "uid": uid,
        "coupleId": couple_id,
        "type": tipe,
        "amount": jumlah,
        "category": kategori,
    }


def get_transaksi_hari_ini(user_phone: str, uid: str = None) -> list:
    """Ambil semua transaksi hari ini untuk user."""
    if not uid:
        user = get_user_by_phone(user_phone)
        if not user:
            return []
        uid = user["uid"]

    db = get_db()
    today_start = datetime.now(WIB).replace(hour=0, minute=0, second=0, microsecond=0)

    docs = (
        db.collection("transactions")
        .where("uid", "==", uid)
        .where("date", ">=", today_start)
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def get_transaksi_bulan_ini(user_phone: str) -> list:
    """Ambil semua transaksi bulan ini untuk user."""
    user = get_user_by_phone(user_phone)
    if not user:
        return []

    db = get_db()
    uid = user["uid"]
    now = datetime.now(WIB)
    bulan_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    docs = (
        db.collection("transactions")
        .where("uid", "==", uid)
        .where("date", ">=", bulan_start)
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def hitung_total_hari_ini(user_phone: str, uid: str = None) -> dict:
    """Hitung total pengeluaran & pemasukan hari ini."""
    transaksi = get_transaksi_hari_ini(user_phone, uid=uid)
    pengeluaran = sum(t["amount"] for t in transaksi if t.get("type") == "expense")
    pemasukan = sum(t["amount"] for t in transaksi if t.get("type") == "income")
    return {"pengeluaran": pengeluaran, "pemasukan": pemasukan, "catatan": transaksi}


def hitung_total_bulan_ini(user_phone: str) -> dict:
    """Hitung total pengeluaran & pemasukan bulan ini."""
    transaksi = get_transaksi_bulan_ini(user_phone)
    pengeluaran = sum(t["amount"] for t in transaksi if t.get("type") == "expense")
    pemasukan = sum(t["amount"] for t in transaksi if t.get("type") == "income")
    return {"pengeluaran": pengeluaran, "pemasukan": pemasukan, "catatan": transaksi}


def get_budget_status(user_phone: str) -> dict | None:
    """
    Cek status anggaran per kategori.
    Returns: {category: {limit, spent, remaining, percentage}}
    """
    user = get_user_by_phone(user_phone)
    if not user:
        return None

    db = get_db()
    uid = user["uid"]
    couple = get_couple(uid)
    if not couple:
        return None

    couple_id = couple["coupleId"]

    # Get budget limits
    budget_doc = db.collection("budgets").document(couple_id).get()
    if not budget_doc.exists:
        return None

    budget_data = budget_doc.to_dict()
    categories = budget_data.get("categories", {})

    if not categories:
        return None

    # Get current month spending per category
    now = datetime.now(WIB)
    bulan_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    docs = (
        db.collection("transactions")
        .where("coupleId", "==", couple_id)
        .where("type", "==", "expense")
        .where("date", ">=", bulan_start)
        .stream()
    )

    spent_by_cat = {}
    for doc in docs:
        data = doc.to_dict()
        cat = data.get("category", "other")
        spent_by_cat[cat] = spent_by_cat.get(cat, 0) + data.get("amount", 0)

    result = {}
    for cat, limit in categories.items():
        if limit and limit > 0:
            spent = spent_by_cat.get(cat, 0)
            result[cat] = {
                "limit": limit,
                "spent": spent,
                "remaining": max(0, limit - spent),
                "percentage": round((spent / limit) * 100, 1),
            }

    return result


def save_ocr_result(user_phone: str, raw_text: str, parsed_items: list):
    """Simpan hasil OCR untuk audit trail."""
    db = get_db()
    doc = {
        "user_phone": user_phone,
        "raw_text": raw_text,
        "parsed_items": parsed_items,
        "timestamp": firestore.SERVER_TIMESTAMP,
        "created_at": datetime.now(WIB).isoformat(),
    }
    db.collection("ocr_results").add(doc)


def get_verified_users_for_reminder() -> list:
    """
    Ambil semua user yang terverifikasi waVerified = True
    dan waReminderEnabled = True (atau default True jika field tidak ada).
    """
    db = get_db()
    users_ref = db.collection("users").where("waVerified", "==", True).stream()
    users = []
    for doc in users_ref:
        data = doc.to_dict()
        data["uid"] = doc.id
        # Hanya kirim jika waReminderEnabled tidak diset False
        if data.get("waReminderEnabled", True):
            users.append(data)
    return users
