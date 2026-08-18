"""
KirimDev Sender — Kirim pesan WhatsApp via KirimDev API
Drop-in replacement untuk Meta Cloud API, payload sama persis.
Endpoint: https://api.kirimdev.com/v1/{PHONE_NUMBER_ID}/messages
Media download: GET /v1/{phone_number_id}/messages/{wamid}/media
"""
import httpx
import logging
import json
from config import KIRIMDEV_API_KEY, KIRIMDEV_BASE_URL, WABA_PHONE_NUMBER_ID, WABA_MEDIA_BASE_URL

logger = logging.getLogger(__name__)

# KirimDev base URL, kompatibel dengan Meta path routing
KIRIMDEV_API_BASE = KIRIMDEV_BASE_URL.rstrip("/")

HEADERS = lambda: {
    "Authorization": f"Bearer {KIRIMDEV_API_KEY}",
    "Content-Type": "application/json",
}


def normalize_phone(phone: str) -> str:
    """Normalize phone number to format 62xxx (no + prefix)."""
    phone = phone.replace("+", "").replace(" ", "").replace("-", "").split(":")[0]
    if phone.startswith("0"):
        phone = "62" + phone[1:]
    elif not phone.startswith("62"):
        phone = "62" + phone
    return phone


async def send_text(phone: str, text: str) -> bool:
    """Kirim pesan teks via KirimDev."""
    phone = normalize_phone(phone)
    url = f"{KIRIMDEV_API_BASE}/{WABA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=HEADERS())
            if resp.status_code in (200, 201):
                data = resp.json()
                # KirimDev wraps response in {"data": {...}} — handle both shapes
                inner = data.get("data", data)
                messages = inner.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id", inner.get("id", ""))
                else:
                    msg_id = inner.get("id", "")
                logger.info(f"KirimDev sent text to {phone}, msg_id={msg_id}")
                return True
            else:
                logger.error(f"KirimDev send failed to {phone}: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"KirimDev error sending to {phone}: {e}", exc_info=True)
        return False


async def send_image(phone: str, image_url: str, caption: str = "") -> bool:
    """Kirim gambar via KirimDev (URL-based — WA server fetch)."""
    phone = normalize_phone(phone)
    url = f"{KIRIMDEV_API_BASE}/{WABA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {"link": image_url},
    }
    if caption:
        payload["image"]["caption"] = caption
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=HEADERS())
            if resp.status_code in (200, 201):
                logger.info(f"KirimDev sent image to {phone}")
                return True
            else:
                logger.error(f"KirimDev image send failed: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"KirimDev error sending image to {phone}: {e}", exc_info=True)
        return False


# ── Media download — KirimDev style ──
# Endpoint: GET /v1/{phone_number_id}/messages/{wamid}/media
# Returns: { data: { url: "..." } } atau direct URL

async def get_media_url(media_id: str, wamid: str = None) -> str:
    """
    Dapatkan download URL untuk media.
    
    KirimDev primary: GET /v1/{phone_id}/messages/{wamid}/media
    Fallback (Meta compat): GET /v1/{media_id} — kalau KirimDev masih proxy Meta langsung.

    Args:
        media_id: media_id (untuk fallback) atau wamid jika wamid tidak provided
        wamid: WhatsApp message ID (X-Kirim-Event-Id / messages[].id)
               Jika diberikan, pakai endpoint KirimDev resmi.
    Returns:
        Download URL string atau empty string jika gagal.
    """
    # Prefer KirimDev native endpoint jika ada wamid
    target_wamid = wamid or media_id

    # 1) Coba KirimDev endpoint: GET /{phone_id}/messages/{wamid}/media
    url_kirimdev = f"{KIRIMDEV_API_BASE}/{WABA_PHONE_NUMBER_ID}/messages/{target_wamid}/media"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url_kirimdev, headers=HEADERS())
            if resp.status_code == 200:
                data = resp.json()
                # KirimDev wraps in {"data": {"url": ...}} or {"url": ...}
                inner = data.get("data", data)
                download_url = inner.get("url", "")
                if download_url:
                    logger.info(f"KirimDev media URL resolved via /messages/{target_wamid}/media")
                    return download_url
                # Kadang response langsung {"data": "https://..."} ?
                if isinstance(data.get("data"), str):
                    return data["data"]
            else:
                logger.warning(
                    f"KirimDev get media URL via /messages/{target_wamid}/media failed: "
                    f"{resp.status_code} {resp.text[:300]}"
                )
    except Exception as e:
        logger.warning(f"KirimDev get media URL exception (primary): {e}")

    # 2) Fallback: Meta-style GET /{media_id} via KirimDev base (mungkin masih di-proxy)
    # Beberapa versi KirimDev masih support GET /{media_id} langsung
    url_fallback = f"{KIRIMDEV_API_BASE}/{media_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url_fallback, headers=HEADERS())
            if resp.status_code == 200:
                data = resp.json()
                inner = data.get("data", data)
                download_url = inner.get("url", "")
                if download_url:
                    logger.info(f"KirimDev media URL resolved via fallback /{{media_id}}")
                    return download_url
            else:
                logger.error(f"KirimDev get media URL fallback failed: {resp.status_code} {resp.text[:300]}")
                return ""
    except Exception as e:
        logger.error(f"KirimDev error getting media URL for {media_id}: {e}", exc_info=True)
        return ""

    return ""


async def download_media(media_url: str) -> bytes:
    """Download media content via KirimDev / lookaside."""
    if not media_url:
        return b""
    try:
        # Untuk URL dari KirimDev API, pakai Bearer token
        # Untuk URL lookaside (cdn.fbsbx.com), token juga bisa via header tapi biasanya URL sudah signed
        headers = HEADERS()
        # Kalau URL adalah fbsbx / fbcdn (Meta CDN), gunakan header tanpa Content-Type
        if "fbsbx.com" in media_url or "fbcdn.net" in media_url or "lookaside" in media_url:
            # Meta CDN URLs already signed, but KirimDev proxy may still need auth
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(media_url, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                # Retry tanpa auth header (URL sudah presigned)
                resp2 = await client.get(media_url)
                if resp2.status_code == 200:
                    return resp2.content
                logger.error(f"KirimDev download media failed (cdn): {resp.status_code}")
                return b""
        else:
            # KirimDev media URL (api.kirimdev.com) — butuh auth
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(media_url, headers=headers)
                if resp.status_code == 200:
                    # Kalau response JSON dengan url lagi, ikuti
                    ctype = resp.headers.get("content-type", "")
                    if "application/json" in ctype:
                        try:
                            j = resp.json()
                            inner = j.get("data", j)
                            nested_url = inner.get("url", "")
                            if nested_url:
                                # Download nested URL
                                resp2 = await client.get(nested_url, headers=headers)
                                if resp2.status_code == 200:
                                    return resp2.content
                                # Try without auth for presigned
                                resp3 = await client.get(nested_url)
                                if resp3.status_code == 200:
                                    return resp3.content
                        except Exception:
                            pass
                    return resp.content
                else:
                    logger.error(f"KirimDev download media failed: {resp.status_code} {resp.text[:200]}")
                    return b""
    except Exception as e:
        logger.error(f"KirimDev error downloading media: {e}", exc_info=True)
        return b""
