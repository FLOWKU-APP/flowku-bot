# Flowku WhatsApp Chatbot Configuration
import os
from dotenv import load_dotenv

load_dotenv()

# WAHA
WAHA_BASE_URL = os.getenv("WAHA_BASE_URL", "http://127.0.0.1:3000")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")

if not WAHA_API_KEY and os.getenv("TESTING") != "true":
    # WAHA_API_KEY not set — might be using WABA instead
    pass

# KirimDev (WhatsApp Cloud API proxy — drop-in replacement for Meta Cloud API)
KIRIMDEV_API_KEY = os.getenv("KIRIMDEV_API_KEY", os.getenv("WABA_ACCESS_TOKEN", ""))
KIRIMDEV_BASE_URL = os.getenv("KIRIMDEV_BASE_URL", "https://api.kirimdev.com/v1")
# WABA_* kept as alias for backwards compat — new deployments should use KIRIMDEV_*
WABA_ACCESS_TOKEN = KIRIMDEV_API_KEY
WABA_PHONE_NUMBER_ID = os.getenv("WABA_PHONE_NUMBER_ID", os.getenv("PHONE_NUMBER_ID", ""))
WABA_BUSINESS_ACCOUNT_ID = os.getenv("WABA_BUSINESS_ACCOUNT_ID", "")
WABA_VERIFY_TOKEN = os.getenv("WABA_VERIFY_TOKEN", "flowku_waba_verify_2026")
WABA_MEDIA_BASE_URL = os.getenv("WABA_MEDIA_BASE_URL", "https://lookaside.fbsbx.com")

# Sender mode: "waba" if KirimDev configured, else "waha"
SENDER_MODE = "waba" if KIRIMDEV_API_KEY and WABA_PHONE_NUMBER_ID else "waha"

# Firestore
FIRESTORE_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID", "flowku-95fb4")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# Gemini OCR API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Gemini fallback models (tried in order — first success wins)
# FREE TIER ONLY (June 2026) — vision/image models only
# Gemini 2.5 Flash: best price-performance for vision tasks (1M TPM)
GEMINI_MODELS = [
    "gemini-2.5-flash",       # Primary: ~15 RPM, 1,500 RPD, 1M TPM
    "gemini-3-flash",         # Fallback 1: newest flash, ~10 RPM
    "gemini-2.5-flash-lite",  # Fallback 2: fastest, cheapest, ~30 RPM
    "gemini-3.1-flash-lite",  # Fallback 3: newest lite
]

# App
APP_PORT = int(os.getenv("APP_PORT", "8700"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "flowku-waha-webhook-2026")

# Reminder times (WIB = UTC+7, so 12:00 WIB = 05:00 UTC, 20:00 WIB = 13:00 UTC)
REMINDER_HOUR_1 = 12  # WIB
REMINDER_HOUR_2 = 20  # WIB

# Owner phone (for receiving reminders)
OWNER_PHONE = os.getenv("OWNER_PHONE", "")  # e.g. "6281234567890"
