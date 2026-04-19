import os
from dotenv import load_dotenv

load_dotenv()

def _get_bool(name, default="true"):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")

# This matches your .env screenshot exactly
APP_IDS = {
    "shopify_temu":    os.getenv("APP_1_ID"),
    "shein":           os.getenv("APP_2_ID"),
    "shopify_temu_eu": os.getenv("APP_3_ID"),
}

LOGIN_URL = os.getenv("LOGIN_URL")

# Per-app credentials. Each app has its OWN password in .env; submitting
# APP_1_PASS while having selected `shein` gets rejected by the backend.
# Key by the app's internal id (the value selected in the dropdown).
CREDENTIALS = {
    os.getenv("APP_1_ID"): (os.getenv("APP_1_USER"), os.getenv("APP_1_PASS")),
    os.getenv("APP_2_ID"): (os.getenv("APP_2_USER"), os.getenv("APP_2_PASS")),
    os.getenv("APP_3_ID"): (os.getenv("APP_3_USER"), os.getenv("APP_3_PASS")),
}
# Strip entries where the app id itself is missing (env not set).
CREDENTIALS = {k: v for k, v in CREDENTIALS.items() if k}

# Kept for backwards compatibility; do NOT use for anything but app #1.
USERNAME = os.getenv("APP_1_USER")
PASSWORD = os.getenv("APP_1_PASS")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# Default to headless so scheduler.py / cron / systemd runs don't pop
# a browser window on the host. For local debugging, set HEADLESS=false
# in .env (or `HEADLESS=false python3 scraper.py`) to watch the scrape
# in a visible Chromium window.
HEADLESS = _get_bool("HEADLESS", "true")