"""
Telegram Bot for PDF management - CORRECTED VERSION
Compatible with Python 3.13 and python-telegram-bot 21.x
With batch support (24 files max) and automatic deletion
English version with Force Join Channel
"""

import os
import sys
import logging
import tempfile
import re
import asyncio
import shutil
import json
import sqlite3
import time
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, UsernameNotOccupied
from pyrogram.enums import ChatMemberStatus

# Import de la configuration
def get_env_or_config(attr, default=None):
    value = os.environ.get(attr)
    if value is not None:
        if attr == "API_ID":
            return int(value)
        return value
    try:
        from config import API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS
        return locals()[attr]
    except Exception:
        return default

API_ID = get_env_or_config("API_ID")
API_HASH = get_env_or_config("API_HASH")
BOT_TOKEN = get_env_or_config("BOT_TOKEN")
ADMIN_IDS = get_env_or_config("ADMIN_IDS", "")

# Timeouts (in seconds) for heavy blocking operations
BANNER_CLEAN_TIMEOUT = int(os.getenv("BANNER_CLEAN_TIMEOUT", "60"))
BANNER_ADD_TIMEOUT = int(os.getenv("BANNER_ADD_TIMEOUT", "60"))

async def run_in_thread_with_timeout(func, *args, timeout: int = 60, **kwargs):
    """Run a blocking function in a thread with an asyncio timeout."""
    return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)

# File to store usernames persistently
USERNAMES_FILE = Path("usernames.json")

# Default username to add at the end of captions
DEFAULT_USERNAME_TAG = "@PdfBot"  # Change according to your bot

# --- Text position preference helpers ---
def get_text_position(user_id: int) -> str:
    """Return user's preferred text position in filename: 'start' or 'end' (default)."""
    return sessions.get(user_id, {}).get('text_position', 'end')

def set_text_position(user_id: int, position: str) -> None:
    """Set user's preferred text position in filename."""
    if position not in ('start', 'end'):
        position = 'end'
    sess = sessions.setdefault(user_id, {})
    sess['text_position'] = position

def is_supported_video(filename):
    """Detects if the file is a supported video"""
    mimetype, _ = mimetypes.guess_type(filename)
    return mimetype and mimetype.startswith("video/")

def clean_caption_with_username(original_caption: str, user_id: int = None) -> str:
    """Cleans the caption and adds the saved username of the user at the end"""
    # Supprimer tous les @usernames existants
    cleaned = re.sub(r"@[\w_]+", "", original_caption).strip()
    
    # Supprimer les espaces doublés
    cleaned = re.sub(r"\s+", " ", cleaned)
    
    # Récupérer le username sauvegardé de l'utilisateur
    if user_id:
        saved_username = get_saved_username(user_id)
        if saved_username:
            # Placer le tag selon la préférence utilisateur
            pos = get_text_position(user_id)
            if pos == 'start':
                final_caption = f"{saved_username} {cleaned}".strip()
            else:
                final_caption = f"{cleaned} {saved_username}".strip()
            return final_caption
    
    # Si pas de username sauvegardé, retourner juste le texte nettoyé
    return cleaned

def is_pdf_file(filename):
    """Detects if the file is a PDF"""
    return filename.lower().endswith('.pdf')

def clean_filename(filename):
    """Cleans the filename by removing blocks with @ or # and emojis"""
    # Supprime tous les blocs [ ] ( ) { } < > contenant @ ou #
    cleaned = re.sub(r'[\[\(\{\<][^)\]\}\>]*[@#][^)\]\}\>]*[\]\)\}\>]', '', filename)
    
    # Supprime les usernames seuls partout (ex: @user, @user_name)
    cleaned = re.sub(r'@[_A-Za-z0-9]+', '', cleaned)
    
    # Supprime tous les emojis
    emoji_pattern = re.compile(
        "["
        u"\U0001F600-\U0001F64F"  # emoticons
        u"\U0001F300-\U0001F5FF"  # symbols & pictographs
        u"\U0001F680-\U0001F6FF"  # transport & map symbols
        u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
        u"\U00002700-\U000027BF"
        u"\U000024C2-\U0001F251"
        "]+", flags=re.UNICODE)
    cleaned = emoji_pattern.sub(r'', cleaned)
    
    # Nettoie les paires de parenthèses/brackets vides restantes
    cleaned = re.sub(r'[\[\(\{\<]\s*[\]\)\}\>]', '', cleaned)
    
    # Nettoie les espaces multiples
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def build_final_filename(user_id: int, original_name: str) -> str:
    """Build the final output filename with the user's tag placed at start or end.
    - Cleans existing usernames and emojis from the base filename.
    - Respects the text position preference stored via `set_text_position()`.
    """
    try:
        base, ext = os.path.splitext(original_name)
        if not ext:
            ext = ".pdf"
        # Clean base name from usernames/emojis/extra spaces
        base = clean_filename(base)

        # Fetch user's saved tag (persistent first, then session fallback)
        username = get_saved_username(user_id) or sessions.get(user_id, {}).get('username', '')
        if not username:
            safe_base = re.sub(r'[\\/:*?"<>|]', '_', base).strip()
            return f"{safe_base}{ext}"

        pos = get_text_position(user_id)
        if pos == 'start':
            new_base = f"{username} {base}".strip()
        else:
            new_base = f"{base} {username}".strip()

        # Sanitize forbidden filename characters
        new_base = re.sub(r'[\\/:*?"<>|]', '_', new_base)
        return f"{new_base}{ext}"
    except Exception as e:
        logger.error(f"build_final_filename error: {e}")
        # Fallback to original name on error
        return original_name

def save_username(user_id, username):
    """Saves a user's username persistently"""
    try:
        # Charger les données existantes
        if USERNAMES_FILE.exists():
            with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        
        # Ajouter/mettre à jour le username
        data[str(user_id)] = username
        
        # Sauvegarder
        with open(USERNAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        
        logger.info(f"💾 Username saved for user {user_id}: {username}")
        return True
    except Exception as e:
        logger.error(f"❌ Error saving username: {e}")
        return False

def get_saved_username(user_id):
    """Retrieves the saved username of a user"""
    try:
        if USERNAMES_FILE.exists():
            with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(str(user_id))
        return None
    except Exception as e:
        logger.error(f"❌ Error reading username: {e}")
        return None

def delete_saved_username(user_id):
    """Deletes the saved username of a user"""
    try:
        if USERNAMES_FILE.exists():
            with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if str(user_id) in data:
                del data[str(user_id)]
                
                with open(USERNAMES_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                
                logger.info(f"🗑️ Username deleted for user {user_id}")
                return True
        return False
    except Exception as e:
        logger.error(f"❌ Error deleting username: {e}")
        return False

# 🔥 CONFIGURATION FORCE JOIN CHANNEL 🔥
# Mode multi-canaux activé (voir helpers get_forced_channels/set_forced_channels)

MAX_FILE_SIZE = 1_400 * 1024 * 1024  # 14 GB
MAX_BATCH_FILES = 24
AUTO_DELETE_DELAY = 300  # 5 minutes

# Messages du bot en anglais
MESSAGES = {
    'start': (
        "👋 Welcome to Advanced PDF Tools Bot!\n\n"
        "Send me a PDF and I'll help you clean, edit, add banner and lock it.\n\n"
        "📋 Features:\n"
        "• Clean usernames in filename\n"
        "• Unlock protected PDFs\n"
        "• Remove pages\n"
        "• Add your default banner\n"
        "• Lock with your default password\n"
        "• Extract a page as image\n"
        "• Sequence processing\n\n"
        "🎯 Commands:\n"
        "/start - Show this message\n"
        "/batch - Enable sequence mode\n"
        "/process - Process sequence files\n"
        "/setbanner - Set your default banner\n"
        "/view_banner - View your banner\n"
        "/setpassword - Set default lock password\n"
        "/reset_password - Change default lock password\n"
        "/setextra_pages - Extract a page as image\n"
        "/setbanied - Enable multi-banner add mode\n"
        "/donebanied - Finish adding banners\n"
        "/viewbanied - View saved banners\n"
        "/deletebanied - Delete a banner by index or all\n"
        "/pdf_edit - Unlock → remove pages → add banner → lock\n"
        "/addfsub - Add forced-subscription channels (admin)\n"
        "/delfsub - Delete forced-subscription channels (admin)\n"
        "/channels - List forced-subscription channels (admin)\n"
        "/status - Check bot status and statistics\n\n"
        "📤 Just send me a PDF to get started!"
    ),
    'not_pdf': "❌ *This is not a PDF file!*",
    'file_too_big': "❌ *File is too large!*",
    'processing': "⏳ _Processing..._",
    'success_unlock': "✅ *PDF unlocked successfully!*",
    'success_pages': "✅ *Pages removed successfully!*",
    'error': "❌ *Error during processing*",
    'force_join': """🚫 *Access Denied!*\n\nTo use this bot, you must first join our official channel:\n👉 @{channel}\n\n✅ Click the button below to join.\nOnce done, click \"I have joined\" to continue.\n\n_Thank you for your support! 💙_"""
}

# Surcharger avec les messages de config.py s'ils existent
try:
    MESSAGES.update(CONFIG_MESSAGES)
except NameError:
    pass  # CONFIG_MESSAGES n'existe pas, on garde les messages par défaut

import pikepdf
import asyncio
from functools import wraps
from typing import List
try:
    import psutil  # facultatif pour RAM/CPU
except Exception:
    psutil = None

# PDF banner cleaning dependencies
try:
    import fitz  # PyMuPDF used in handlers and banner conversion
except Exception:
    fitz = None

# Pillow (for converting image banner to PDF on the fly)
try:
    from PIL import Image
except Exception:
    Image = None

try:
    from utils.banner_cleaner import clean_pdf_banners
except Exception:
    # Fallback no-op if the utility is unavailable
    def clean_pdf_banners(pdf_bytes: bytes, user_id: int, base_dir: str | Path = "data/banied") -> bytes:
        return pdf_bytes

START_TIME = time.time()

USERS_FILE = Path("users.json")
FJ_FILE = Path("force_join_channels.json")
STATS_FILE = Path("stats.json")
DB_FILE = Path("bot_data.sqlite3")

def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {path.name}: {e}")

# ==== Persistent storage (SQLite) for users and stats ====
def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    files INTEGER NOT NULL DEFAULT 0,
                    storage_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("INSERT OR IGNORE INTO stats(id, files, storage_bytes) VALUES (1, 0, 0)")
    except Exception as e:
        logger.error(f"Error initializing DB: {e}")

def migrate_json_to_db():
    """One-time migration from users.json and stats.json if DB is empty."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            # Migrate users
            (user_count_db,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            if user_count_db == 0 and USERS_FILE.exists():
                data = _load_json(USERS_FILE, {"users": []})
                users = [int(u) for u in data.get("users", []) if str(u).strip()]
                if users:
                    conn.executemany("INSERT OR IGNORE INTO users(id) VALUES (?)", [(u,) for u in users])
            # Migrate stats
            row = conn.execute("SELECT files, storage_bytes FROM stats WHERE id=1").fetchone()
            if row is not None and row[0] == 0 and row[1] == 0 and STATS_FILE.exists():
                s = _load_json(STATS_FILE, {"files": 0, "storage_bytes": 0})
                files = int(s.get("files", 0) or 0)
                storage_bytes = int(s.get("storage_bytes", 0) or 0)
                conn.execute(
                    "UPDATE stats SET files = ?, storage_bytes = ? WHERE id=1",
                    (files, storage_bytes),
                )
    except Exception as e:
        logger.error(f"Error migrating JSON to DB: {e}")

# Initialize DB and attempt migration on startup
init_db()
migrate_json_to_db()

def track_user(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (int(user_id),))
    except Exception as e:
        logger.error(f"Error tracking user in DB: {e}")

def total_users() -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return int(n)
    except Exception as e:
        logger.error(f"Error counting users in DB: {e}")
        data = _load_json(USERS_FILE, {"users": []})
        return len(data.get("users", []))

def get_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT files, storage_bytes FROM stats WHERE id=1").fetchone()
            if row is None:
                return {"files": 0, "storage_bytes": 0}
            return {"files": int(row[0]), "storage_bytes": int(row[1])}
    except Exception as e:
        logger.error(f"Error reading stats from DB: {e}")
        return {"files": 0, "storage_bytes": 0}

def bump_stats(file_path: str | None):
    add_bytes = 0
    try:
        if file_path and os.path.exists(file_path):
            add_bytes = os.path.getsize(file_path)
    except Exception:
        add_bytes = 0
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "UPDATE stats SET files = files + 1, storage_bytes = storage_bytes + ? WHERE id=1",
                (int(add_bytes),),
            )
    except Exception as e:
        logger.error(f"Error updating stats in DB: {e}")

def format_bytes(n: int) -> str:
    for unit in ["B","KB","MB","GB","TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"

def is_admin(user_id: int) -> bool:
    admin_list = [int(x) for x in str(ADMIN_IDS).split(',') if x.strip()] if ADMIN_IDS else []
    return user_id in admin_list

def admin_only(func):
    async def wrapper(client, message, *args, **kwargs):
        if not is_admin(message.from_user.id):
            await client.send_message(message.chat.id, "❌ Admins only.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper

try:
    # This is for the timeout decorator
    import signal
except ImportError:
    signal = None

# ... (le reste des imports)


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize sessions and user batches
sessions = {}
user_batches = {}  # {user_id: [docs]}
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)
cleanup_task_started = False  # Flag pour éviter de démarrer plusieurs fois la tâche

def ensure_session_dict(user_id: int) -> dict:
    """Ensure a session dict exists for the given user_id and return it."""
    sess = sessions.get(user_id)
    if sess is None:
        sess = {}
        sessions[user_id] = sess
    return sess

# --- Processing flag helpers with watchdog ---
# Global watchdog timeout for any processing operation
GLOBAL_PROCESS_MAX = int(os.getenv("GLOBAL_PROCESS_MAX", "180"))

def set_processing_flag(user_id: int, chat_id: int | None = None, source: str = "") -> None:
    """Set the processing flag with logging and start a watchdog to auto-clear on timeout."""
    sess = ensure_session_dict(user_id)
    # Cancel an existing watchdog if present
    try:
        task = sess.get('processing_watchdog')
        if task and not task.done():
            task.cancel()
    except Exception:
        pass
    sess['processing'] = True
    sess['processing_started'] = time.time()
    if source:
        sess['processing_source'] = source
    if chat_id is not None:
        sess['processing_chat_id'] = chat_id
    logger.info(f"[processing] SET user=%s source=%s", user_id, sess.get('processing_source', source))
    # Start watchdog
    try:
        sess['processing_watchdog'] = asyncio.create_task(_processing_watchdog(user_id))
    except Exception as e:
        logger.warning(f"[processing] Failed to start watchdog for user {user_id}: {e}")

def clear_processing_flag(user_id: int, source: str = "", reason: str = "") -> None:
    """Clear the processing flag with logging and cancel any watchdog."""
    sess = ensure_session_dict(user_id)
    # Cancel watchdog
    try:
        task = sess.pop('processing_watchdog', None)
        if task and not task.done():
            task.cancel()
    except Exception:
        pass
    started = sess.get('processing_started') or time.time()
    elapsed = time.time() - started if started else 0
    sess['processing'] = False
    # Do not remove processing_source/chat_id to allow post-mortem logs unless explicitly cleaned elsewhere
    logger.info(
        f"[processing] CLEAR user=%s source=%s elapsed=%.2fs reason=%s",
        user_id,
        sess.get('processing_source', source),
        elapsed,
        reason or "",
    )

async def _processing_watchdog(user_id: int):
    """Auto-clear processing flag if it exceeds GLOBAL_PROCESS_MAX seconds."""
    try:
        await asyncio.sleep(GLOBAL_PROCESS_MAX)
    except Exception:
        return
    sess = ensure_session_dict(user_id)
    try:
        if sess.get('processing'):
            started = sess.get('processing_started') or (time.time() - GLOBAL_PROCESS_MAX)
            elapsed = time.time() - started
            # Only clear if elapsed >= timeout (protect against race conditions)
            if elapsed >= GLOBAL_PROCESS_MAX:
                src = sess.get('processing_source', 'unknown')
                logger.warning(f"[processing] WATCHDOG CLEAR user={user_id} source={src} elapsed={elapsed:.2f}s")
                sess['processing'] = False
                # Notify user if possible
                chat_id = sess.get('processing_chat_id')
                if chat_id:
                    try:
                        await app.send_message(chat_id, "⏱️ Previous operation timed out. I've reset your session. Please try again.")
                    except Exception:
                        pass
    except Exception as e:
        logger.warning(f"[processing] Watchdog error for user {user_id}: {e}")

def clear_user_batch(user_id: int) -> None:
    """Reset the user's batch list safely without raising KeyError."""
    user_batches[user_id] = []

async def cleanup_temp_files():
    """Background task: periodically remove old files from TEMP_DIR and prune empty dirs."""
    # Defaults: run every 10 minutes, delete files older than 2 hours
    try:
        interval = int(os.getenv("TEMP_CLEANUP_INTERVAL", "600"))
    except Exception:
        interval = 600
    try:
        max_age = int(os.getenv("TEMP_FILE_MAX_AGE", "7200"))
    except Exception:
        max_age = 7200

    while True:
        try:
            now = time.time()
            # Iterate user temp directories
            for entry in TEMP_DIR.iterdir():
                if not entry.is_dir():
                    continue
                # Clean files in user dir
                try:
                    for p in entry.iterdir():
                        try:
                            if p.is_file():
                                age = now - p.stat().st_mtime
                                if age > max_age:
                                    p.unlink(missing_ok=True)
                            elif p.is_dir():
                                # Optional: remove nested empty dirs
                                try:
                                    next(p.iterdir())
                                except StopIteration:
                                    p.rmdir()
                        except Exception:
                            # Best effort cleanup; continue
                            pass
                    # Remove user dir if empty
                    try:
                        next(entry.iterdir())
                    except StopIteration:
                        entry.rmdir()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"cleanup_temp_files error: {e}")

        try:
            await asyncio.sleep(interval)
        except Exception:
            # If event loop sleep fails, avoid busy loop
            await asyncio.sleep(600)

# Multi-banner storage (per user)
BANIED_BASE_DIR = Path("data") / "banied"
BANIED_BASE_DIR.mkdir(parents=True, exist_ok=True)
# Tracks users currently adding banners
BANIED_ADD_MODE: set[int] = set()

def ensure_banied_dir(user_id: int) -> Path:
    p = BANIED_BASE_DIR / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p

def list_banied_images(user_id: int) -> list[Path]:
    p = ensure_banied_dir(user_id)
    imgs = [f for f in p.iterdir() if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}]
    return sorted(imgs)

def delete_banied(user_id: int, index: int | None = None) -> int:
    """Delete one (1-based index) or all banners. Returns number deleted."""
    files = list_banied_images(user_id)
    if not files:
        return 0
    if index is None:
        count = 0
        for f in files:
            try:
                f.unlink(missing_ok=True)
                count += 1
            except Exception:
                pass
        return count
    else:
        if 1 <= index <= len(files):
            try:
                files[index - 1].unlink(missing_ok=True)
                return 1
            except Exception:
                return 0
        return 0

# PDF user settings and helpers (banner/password/extract)
BANNERS_DIR = Path("banners")
BANNERS_DIR.mkdir(exist_ok=True)
PDF_SETTINGS_FILE = Path("pdf_settings.json")

def _load_pdf_settings():
    try:
        if PDF_SETTINGS_FILE.exists():
            with open(PDF_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading PDF settings: {e}")
    return {}

def _save_pdf_settings(data: dict) -> None:
    try:
        with open(PDF_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving PDF settings: {e}")

def get_user_pdf_settings(user_id: int) -> dict:
    data = _load_pdf_settings()
    return data.get(str(user_id), {"banner_path": None, "lock_password": None})

def update_user_pdf_settings(user_id: int, **patch) -> dict:
    data = _load_pdf_settings()
    current = data.get(str(user_id), get_user_pdf_settings(user_id))
    current.update(patch)
    data[str(user_id)] = current
    _save_pdf_settings(data)
    return current

def _image_to_pdf(img_path: str) -> str:
    if Image is None:
        raise RuntimeError("Pillow not installed. Please `pip install pillow`.")
    out_pdf = BANNERS_DIR / (Path(img_path).stem + ".pdf")
    with Image.open(img_path) as im:
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(out_pdf, "PDF", resolution=100.0)
    return str(out_pdf)

def _ensure_banner_pdf_path(user_id: int) -> str | None:
    banner_path = get_user_pdf_settings(user_id).get("banner_path")
    if not banner_path or not os.path.exists(banner_path):
        return None
    return banner_path if banner_path.lower().endswith(".pdf") else _image_to_pdf(banner_path)

def add_banner_pages_to_pdf(in_pdf: str, out_pdf: str, banner_pdf: str, place: str = "before") -> None:
    with pikepdf.open(in_pdf) as pdf, pikepdf.open(banner_pdf) as banner:
        banner_pages = list(banner.pages)
        if place in ("before", "both", None, ""):
            for p in reversed(banner_pages):
                pdf.pages.insert(0, p)
        if place in ("after", "both"):
            for p in banner_pages:
                pdf.pages.append(p)
        pdf.save(out_pdf)

def lock_pdf_with_password(in_pdf: str, out_pdf: str, password: str) -> None:
    with pikepdf.open(in_pdf) as pdf:
        enc = pikepdf.Encryption(user=password, owner=password, R=4)
        pdf.save(out_pdf, encryption=enc)

def extract_page_to_png(pdf_path: str, page_number_1based: int, out_png: str, zoom: float = 2.0) -> str:
    if fitz is None:
        raise RuntimeError("PyMuPDF not installed. Please `pip install pymupdf`.")
    with fitz.open(pdf_path) as doc:
        if page_number_1based < 1 or page_number_1based > len(doc):
            raise ValueError(f"Page {page_number_1based} out of bounds (1..{len(doc)})")
        page = doc[page_number_1based - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(out_png)
    return out_png

def parse_pages_spec(spec: str) -> list[int]:
    spec = (spec or "").strip().lower()
    if not spec or spec in {"none", "0", "no", "non", "skip"}:
        return []
    pages: set[int] = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            if a.isdigit() and b.isdigit():
                a_i, b_i = int(a), int(b)
                if a_i <= b_i:
                    pages.update(range(a_i, b_i + 1))
        elif chunk.isdigit():
            pages.add(int(chunk))
    return sorted(p for p in pages if p >= 1)

def parse_pages_text(text: str) -> tuple[list[int], str | None]:
    """Parse a user-provided pages string into a list of 1-based page numbers.
    Returns (pages, error). Error is None if parsing succeeded.
    Accepts formats like: "1,3-5". Returns [] for empty/none-like input.
    """
    spec = (text or "").strip()
    if not spec:
        return [], None
    # Quick validation: allow digits, comma, dash and spaces only
    if not re.fullmatch(r"[\d,\-\s]+", spec):
        return [], "Invalid pages format. Use numbers, commas and dashes (e.g. 1,3-5)."
    pages = parse_pages_spec(spec)
    if not pages and spec and spec not in {"0", "none", "no", "non", "skip"}:
        return [], "No valid pages found. Example: 1,3-5"
    return pages, None

def get_full_pages_buttons(user_id: int):
    """Build the inline keyboard for Full Process page selection.
    Provides quick options: First, Last, Middle, and Manual entry.
    """
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"full_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"full_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"full_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("Keep all pages (None)", callback_data=f"full_none:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"full_manual:{user_id}"),
        ],
    ])
    return keyboard

def get_pdf_edit_pages_buttons(user_id: int):
    """Inline keyboard for /pdf_edit page selection (First/Last/Middle/Manual)."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"edit_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"edit_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"edit_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"edit_manual:{user_id}"),
        ],
    ])
    return keyboard

def get_remove_pages_buttons(user_id: int):
    """Inline keyboard for The Both -> Remove Pages (First/Last/Middle/Manual)."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"both_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"both_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"both_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"both_manual:{user_id}"),
        ],
    ])
    return keyboard

def get_batch_both_pages_buttons(user_id: int):
    """Inline keyboard for Batch 'The Both' page selection (First/Last/Middle/Manual)."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"batch_both_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"batch_both_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"batch_both_middle:{user_id}"),
        ],
        [
            InlineKeyboardButton("📝 Enter manually", callback_data=f"batch_both_manual:{user_id}"),
        ],
    ])
    return keyboard

def unlock_pdf(in_pdf: str, out_pdf: str, password: str) -> None:
    with pikepdf.open(in_pdf, password=password) as pdf:
        pdf.save(out_pdf)

def remove_pages_by_numbers(in_pdf: str, out_pdf: str, one_based_pages: list[int]) -> None:
    with pikepdf.open(in_pdf) as pdf:
        if not one_based_pages:
            pdf.save(out_pdf)
            return
        n = len(pdf.pages)
        for p in sorted(set(one_based_pages), reverse=True):
            if 1 <= p <= n:
                del pdf.pages[p - 1]
        if len(pdf.pages) == 0:
            raise ValueError("All pages were removed; result would be empty.")
        pdf.save(out_pdf)

# Protection globale contre les doublons et rate limiting
processed_messages = {}
user_last_command = {}  # {user_id: (command, timestamp)}
user_actions = {}  # Pour le rate limiting

def check_rate_limit(user_id):
    """Vérifie si l'utilisateur n'abuse pas"""
    # 🔥 Si l'utilisateur est en mode batch, augmenter la limite
    session = sessions.get(user_id, {})
    if session.get('batch_mode'):
        # En mode batch, permettre jusqu'à 100 actions par minute
        rate_limit = 100
    else:
        rate_limit = 30
    
    current_time = datetime.now()
    # Nettoyer les anciennes actions
    user_actions[user_id] = [
        t for t in user_actions.get(user_id, []) 
        if (current_time - t).seconds < 60
    ]
    
    if len(user_actions[user_id]) >= rate_limit:
        logger.warning(f"⚠️ Rate limit atteint pour user {user_id}")
        return False  # Trop d'actions
    
    user_actions[user_id].append(current_time)
    return True

def is_duplicate_message(user_id, message_id, command_type="message"):
    """Vérifie si un message a déjà été traité ou si c'est une commande répétée"""
    current_time = datetime.now()
    
    # Vérifier le rate limit
    if not check_rate_limit(user_id):
        return "rate_limit"
    
    # Protection contre les commandes répétées (même utilisateur, même commande, < 2 secondes)
    if command_type in ["start", "batch", "process"]:
        if user_id in user_last_command:
            last_cmd, last_time = user_last_command[user_id]
            if last_cmd == command_type and (current_time - last_time).total_seconds() < 2:
                logger.info(f"Command {command_type} ignored - repeated too quickly for user {user_id}")
                return True
        user_last_command[user_id] = (command_type, current_time)
    
    # Protection par message_id (pour les messages uniques)
    key = f"{user_id}_{message_id}"
    
    # Nettoyer les anciens messages (plus de 5 minutes)
    keys_to_remove = []
    for k, timestamp in processed_messages.items():
        if (current_time - timestamp).seconds > 300:
            keys_to_remove.append(k)
    for k in keys_to_remove:
        del processed_messages[k]
    
    # Vérifier si le message est un doublon
    if key in processed_messages:
        return "duplicate"
    
    processed_messages[key] = current_time
    return False

async def send_limit_message(client, chat_id, limit_type):
    """Envoie un message d'information selon le type de limite atteinte"""
    if limit_type == "rate_limit":
        await client.send_message(
            chat_id,
            "⛔️ **Limite atteinte** : Tu ne peux envoyer que 30 fichiers par minute.\n\n"
            "⏰ Réessaie dans quelques secondes."
        )
    elif limit_type == "duplicate":
        await client.send_message(
            chat_id,
            "⚠️ **Fichier déjà traité** : Ce fichier a déjà été traité récemment.\n\n"
            "⏰ Attends 5 minutes avant de le renvoyer."
        )

def reset_session_flags(user_id):
    """Réinitialise les flags temporaires de session (ne touche pas batch_mode)"""
    if user_id in sessions:
        sessions[user_id].pop('just_processed', None)

async def set_just_processed_flag(user_id, delay=1):
    """Marque qu'un fichier vient d'être traité et réinitialise le flag après un délai"""
    if user_id in sessions:
        sessions[user_id]['just_processed'] = True
        
        # Réinitialiser le flag après le délai
        async def reset_flag():
            await asyncio.sleep(delay)
            if user_id in sessions:
                sessions[user_id]['just_processed'] = False
        
        asyncio.create_task(reset_flag())

app = Client(
    "pdfbot-dev",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

""" FORCE JOIN (multi-channels, persistant) """
DEFAULT_FORCE_JOIN = []  # ex: ["djd208"]
FORCE_JOIN_REQUIRE_ALL = False

def get_forced_channels() -> List[str]:
    data = _load_json(FJ_FILE, {"channels": DEFAULT_FORCE_JOIN})
    chans: list[str] = []
    for ch in data.get("channels", []):
        c = str(ch).strip().lstrip("@").lstrip("#")
        if c:
            if c not in chans:
                chans.append(c)
    data["channels"] = chans
    _save_json(FJ_FILE, data)
    return chans

def set_forced_channels(channels: List[str]):
    norm: list[str] = []
    for ch in channels:
        c = str(ch).strip().lstrip("@").lstrip("#")
        if c and c not in norm:
            norm.append(c)
    _save_json(FJ_FILE, {"channels": norm})

def add_forced_channels(channels: List[str]) -> List[str]:
    current = set(get_forced_channels())
    for ch in channels:
        c = str(ch).strip().lstrip("@").lstrip("#")
        if c:
            current.add(c)
    set_forced_channels(list(current))
    return get_forced_channels()

def del_forced_channels(channels: List[str]) -> List[str]:
    current = set(get_forced_channels())
    for ch in channels:
        c = str(ch).strip().lstrip("@").lstrip("#")
        if c in current:
            current.remove(c)
    set_forced_channels(list(current))
    return get_forced_channels()

async def is_user_in_channel(user_id):
    # Admins bypass
    if is_admin(user_id):
        return True

    channels = get_forced_channels()
    if not channels:
        return True

    valid_statuses = [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ]

    async def in_one(ch: str) -> bool:
        try:
            member = await app.get_chat_member(ch, user_id)
            return member.status in valid_statuses
        except UserNotParticipant:
            return False
        except ChatAdminRequired:
            return True
        except UsernameNotOccupied:
            return True
        except Exception as e:
            logger.error(f"check member error @{ch}: {e}")
            return True

    results = []
    for ch in channels:
        results.append(await in_one(ch))
    return all(results) if FORCE_JOIN_REQUIRE_ALL else any(results)

async def send_force_join_message(client, message):
    channels = get_forced_channels()
    if not channels:
        return
    rows = [[InlineKeyboardButton(f"📢 Join @{ch}", url=f"https://t.me/{ch}")] for ch in channels]
    rows.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined")])
    txt = (
        "🚫 *Access Denied!*\n\nTo use this bot, you must first join our channel(s):\n"
        + "\n".join([f"👉 @{c}" for c in channels])
        + "\n\n✅ Click the button(s) above to join.\nOnce done, tap I have joined to continue.\n\n_Thank you for your support!_"
    )
    await client.send_message(message.chat.id, txt, reply_markup=InlineKeyboardMarkup(rows))

def get_user_temp_dir(user_id):
    """Retourne le dossier temporaire spécifique à l'utilisateur"""
    user_dir = TEMP_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)
    return user_dir

def clean_text(text):
    """Nettoie le texte en supprimant toutes les variantes de @username et hashtags"""
    if not text:
        return text
    text = re.sub(r'[\[\(\{]?@\w+[\]\)\}]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'#\w+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_and_clean_pdf_text(page):
    """Extrait et nettoie le texte d'une page PDF"""
    try:
        text = page.extract_text()
        if text:
            return clean_text(text)
    except Exception as e:
        logger.warning(f"Error extracting text: {e}")
    return ""

async def safe_edit_message(message, text):
    """Édite un message en évitant l'erreur MessageNotModified"""
    try:
        await message.edit_message_text(text)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            # Ignorer cette erreur, c'est normal
            logger.debug(f"Message non modifié (même contenu): {e}")
        else:
            logger.error(f"Error editing message: {e}")
            raise e

async def create_or_edit_status(client, origin, text: str):
    """Create a new status message in the current chat and return it.
    Works with both Message and CallbackQuery.message origins.
    """
    # Resolve chat id from origin (Message or CallbackQuery.message)
    chat_id = None
    try:
        chat_id = origin.chat.id
    except Exception:
        try:
            chat_id = origin.message.chat.id
        except Exception:
            chat_id = None
    if chat_id is None:
        logger.error("create_or_edit_status: unable to resolve chat_id from origin")
        raise ValueError("Cannot resolve chat_id for status message")
    try:
        return await client.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"create_or_edit_status error: {e}")
        raise

async def send_and_delete(client, chat_id, file_path, file_name, caption=None, delay_seconds=AUTO_DELETE_DELAY):
    """Envoie un document et le supprime automatiquement après un délai"""
    try:
        logger.info(f"📤 send_and_delete - Fichier: {file_path} - Existe: {os.path.exists(file_path)}")
        logger.info(f"📤 send_and_delete - Chat: {chat_id} - Nom: {file_name} - Délai: {delay_seconds}")
        
        # Marquer qu'on vient de traiter un fichier
        await set_just_processed_flag(chat_id)
        
        with open(file_path, 'rb') as f:
            # Ici on ne rajoute pas la phrase de suppression à la caption !
            sent = await client.send_document(
                chat_id, 
                document=f,
                file_name=file_name,
                caption=caption or ""
                # PAS de reply_markup=keyboard ici !
            )
            logger.info(f"✅ Document envoyé avec succès - ID: {sent.id}")
            # Statistiques de traitement
            bump_stats(file_path)

            # Planifier la suppression
            async def delete_after_delay():
                await asyncio.sleep(delay_seconds)
                try:
                    await sent.delete()
                    logger.info(f"Message deleted after {delay_seconds}s")
                except Exception as e:
                    logger.error(f"Error deleting message: {e}")
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Local file deleted: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting file: {e}")

            if delay_seconds > 0:
                asyncio.create_task(delete_after_delay())

    except Exception as e:
        logger.error(f"Error send_and_delete: {e}")
    finally:
        # Le flag est géré par set_just_processed_flag
        pass

async def send_optimized_pdf(client, chat_id, original_file_path, new_file_name, caption=None, delay_seconds=AUTO_DELETE_DELAY):
    """
    Envoie un PDF de manière optimisée :
    - Si seul le nom change, utilise le fichier original
    - Sinon, utilise le fichier modifié
    """
    try:
        # Vérifier si le fichier original existe et est différent du nouveau nom
        if (os.path.exists(original_file_path) and 
            os.path.basename(original_file_path) != new_file_name):
            
            # Optimisation : utiliser le fichier original avec le nouveau nom
            logger.info(f"🚀 Optimisation: utilisation du fichier original avec nouveau nom")
            await send_and_delete(client, chat_id, original_file_path, new_file_name, caption, delay_seconds)
        else:
            # Utiliser le fichier modifié normalement
            logger.info(f"📤 Envoi normal du fichier modifié")
            await send_and_delete(client, chat_id, original_file_path, new_file_name, caption, delay_seconds)
            
    except Exception as e:
        logger.error(f"Error send_optimized_pdf: {e}")
        # Fallback vers la méthode normale
        await send_and_delete(client, chat_id, original_file_path, new_file_name, caption, delay_seconds)

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    track_user(message.from_user.id)
    global cleanup_task_started
    user_id = message.from_user.id
    
    # S'assurer que la session existe et mettre à jour l'activité
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()

    # If user is in banner-add mode, ignore normal document flow
    if user_id in BANIED_ADD_MODE:
        await client.send_message(message.chat.id, "🪧 Tu es en mode ajout de bannières. Envoie des images/PDF ou /donebanied pour terminer.")
        return
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # === COOLDOWN /START (2 secondes) ===
    if not hasattr(start_handler, 'last_used'):
        start_handler.last_used = {}
    
    now = time.time()
    last = start_handler.last_used.get(user_id, 0)
    if now - last < 2:  # 2 secondes de cooldown
        logger.info(f"⏳ Cooldown /start pour user {user_id} - attendre 2s")
        return  # Message silencieux pour éviter le spam
    start_handler.last_used[user_id] = now
    # === FIN COOLDOWN ===
    
    # DEBUG EXPRESS - Vérifier les doubles instances
    print(f"DEBUG START: Appel handler /start pour user {user_id} à {datetime.now()}")
    
    # Protection anti-doublon et rate limit
    duplicate_check = is_duplicate_message(user_id, message.id, "start")
    if duplicate_check:
        if duplicate_check == "rate_limit":
            await send_limit_message(client, message.chat.id, "rate_limit")
        elif duplicate_check == "duplicate":
            await send_limit_message(client, message.chat.id, "duplicate")
        logger.info(f"Start command ignored - {duplicate_check} for user {user_id}")
        return
    
    logger.info(f"Start command received from user {user_id}")
    
    # Start the cleanup task on first call
    if not cleanup_task_started:
        asyncio.create_task(cleanup_temp_files())
        cleanup_task_started = True
        logger.info("Periodic cleanup task started")
        
        # Send startup message to all users on first /start
        try:
            await startup_message()
        except Exception as e:
            logger.error(f"Error sending startup message: {e}")
    
    # NEW: Load saved username
    saved_username = get_saved_username(user_id)
    
    # Réinitialiser complètement la session
    delete_delay = session.get('delete_delay', AUTO_DELETE_DELAY)
    
    clear_user_batch(user_id)
    session.clear()
    
    # NEW: Restore username from file
    if saved_username:
        session['username'] = saved_username
        logger.info(f"📂 Username restored from file for user {user_id}: {saved_username}")
    
    if delete_delay != AUTO_DELETE_DELAY:
        session['delete_delay'] = delete_delay
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📦 Sequence Mode", callback_data="batch_mode")]
    ])
    
    await client.send_message(message.chat.id, MESSAGES['start'], reply_markup=keyboard)

@app.on_message(filters.command("batch") & filters.private)
async def batch_command(client, message: Message):
    user_id = message.from_user.id
    track_user(user_id)
    
    # S'assurer que la session existe et mettre à jour l'activité
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon et rate limit
    duplicate_check = is_duplicate_message(user_id, message.id, "batch")
    if duplicate_check:
        if duplicate_check == "rate_limit":
            await send_limit_message(client, message.chat.id, "rate_limit")
        elif duplicate_check == "duplicate":
            await send_limit_message(client, message.chat.id, "duplicate")
        logger.info(f"Batch command ignored - {duplicate_check} for user {user_id}")
        return
    
    logger.info(f"🔍 batch_command called - User {user_id} - Time: {datetime.now()}")
    
    # Protection against double calls
    if session.get('batch_command_processing'):
        logger.info(f"🔍 batch_command ignored - already in progress for user {user_id}")
        return
    
    session['batch_command_processing'] = True
    
    # 🔥 IMPORTANT: Activer le mode batch
    session['batch_mode'] = True
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    count = len(user_batches[user_id])
    if count > 0:
        await client.send_message(
            message.chat.id,
            f"📦 **Sequence Mode**\n\n"
            f"✅ You have {count} file(s) waiting\n"
            f"📊 Maximum: {MAX_BATCH_FILES} files\n\n"
            f"🎥 Videos are downloaded immediately to avoid session expiration\n"
            f"📄 PDFs will be processed when you send `/process`\n\n"
            f"🔄 Send `/process` to process all files"
        )
    else:
        await client.send_message(
            message.chat.id,
            f"📦 **Sequence Mode**\n\n"
            f"📭 No files waiting\n\n"
            f"✅ You can send up to {MAX_BATCH_FILES} files\n"
            f"🎥 Videos will be downloaded immediately to avoid session expiration\n"
            f"📄 PDFs will be processed when you send `/process`\n\n"
            f"⏰ **Important**: Videos must be processed within 1-2 minutes of sending\n"
            f"🔄 Send `/process` when you're done adding files"
        )
    
    # Libérer le flag
    session['batch_command_processing'] = False

@app.on_message(filters.command("process") & filters.private)
async def process_batch_command(client, message: Message):
    user_id = message.from_user.id
    track_user(user_id)
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon et rate limit
    duplicate_check = is_duplicate_message(user_id, message.id, "process")
    if duplicate_check:
        if duplicate_check == "rate_limit":
            await send_limit_message(client, message.chat.id, "rate_limit")
        elif duplicate_check == "duplicate":
            await send_limit_message(client, message.chat.id, "duplicate")
        logger.info(f"Process command ignored - {duplicate_check} for user {user_id}")
        return
    
    logger.info(f"🔍 process_batch_command called - User {user_id} - Time: {datetime.now()}")
    
    # Protection against double calls
    if sessions.get(user_id, {}).get('process_command_processing'):
        logger.info(f"🔍 process_batch_command ignored - already in progress for user {user_id}")
        return
    
    sessions[user_id] = sessions.get(user_id, {})
    sessions[user_id]['process_command_processing'] = True
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    # 🔥 LOG DEBUG
    logger.info(f"📦 BATCH STATUS: User {user_id} has {len(user_batches[user_id])} files in batch")
    
    batch_files = user_batches[user_id]
    if not batch_files:
        await client.send_message(message.chat.id, "❌ No files waiting in the batch")
        sessions[user_id]['process_command_processing'] = False
        return
    
    # 1️⃣ D'abord : traiter toutes les vidéos du batch
    for entry in batch_files[:]:  # [:] fait une copie pour suppression sûre
        if entry.get('is_video'):
            file_id = entry['file_id']
            original_caption = entry.get('caption', '') or entry.get('file_name', '')
            # Nettoyer la caption + username personnalisé
            final_caption = clean_caption_with_username(original_caption, user_id)
            try:
                sent = await client.send_video(
                    chat_id=message.chat.id,
                    video=file_id,
                    caption=final_caption
                )
                delay = sessions.get(user_id, {}).get('delete_delay', AUTO_DELETE_DELAY)
                if delay > 0:
                    async def delete_after_delay():
                        await asyncio.sleep(delay)
                        try:
                            await sent.delete()
                        except Exception:
                            pass
                    asyncio.create_task(delete_after_delay())
            except Exception as e:
                await client.send_message(message.chat.id, f"❌ Error sending video: {str(e)}")
            # Supprimer la vidéo traitée du batch
            batch_files.remove(entry)

    # 2️⃣ Ensuite : afficher le menu si des PDF restent dans la séquence
    pdf_files = [f for f in batch_files if not f.get('is_video')]
    if pdf_files:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Clean usernames (all)", callback_data=f"batch_clean:{user_id}")],
            [InlineKeyboardButton("🔓 Unlock all", callback_data=f"batch_unlock:{user_id}")],
            [InlineKeyboardButton("🗑️ Remove pages (all)", callback_data=f"batch_pages:{user_id}")],
            [InlineKeyboardButton("🛠️ The Both (all)", callback_data=f"batch_both:{user_id}")],
            [InlineKeyboardButton("⚡ Full Process (all)", callback_data=f"batch_fullproc:{user_id}")],
            [InlineKeyboardButton("🪧 Add banner (all)", callback_data=f"batch_add_banner:{user_id}")],
            [InlineKeyboardButton("🔐 Lock all", callback_data=f"batch_lock:{user_id}")],
            [InlineKeyboardButton("🧹 Clear sequence", callback_data=f"batch_clear:{user_id}")]
        ])
        await client.send_message(
            message.chat.id,
            f"📦 **Sequence Processing**\n\n"
            f"{len(pdf_files)} PDF(s) ready\n\n"
            f"What do you want to do?",
            reply_markup=keyboard
        )
        sessions[user_id]['process_command_processing'] = False
        return

    # 3️⃣ S'il ne reste plus de PDF ni vidéo : tout a été traité
    await client.send_message(
        message.chat.id,
        "✅ All videos processed!\n\nSend more files or /start to exit sequence mode."
    )
    clear_user_batch(user_id)
    sessions[user_id]['process_command_processing'] = False

def build_pdf_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    # Include a button to change text position preference
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clean usernames", callback_data=f"clean_username:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
        [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}")],
        [InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
    ])

def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Settings/parameters menu for per-user options."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Change Position", callback_data=f"change_position:{user_id}")],
        [InlineKeyboardButton("➕ Add/Edit Hashtag", callback_data="add_hashtag")],
        [InlineKeyboardButton("🗑️ Remove Hashtag", callback_data="delete_username")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_settings:{user_id}")],
    ])

@app.on_callback_query(filters.regex(r"^change_position:(\d+)$"))
async def cb_change_position(client, query: CallbackQuery):
    try:
        user_id = query.from_user.id
        current = get_text_position(user_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📍 At Start{' ✓' if current=='start' else ''}", callback_data=f"set_position_start:{user_id}")],
            [InlineKeyboardButton(f"📍 At End{' ✓' if current=='end' else ''}", callback_data=f"set_position_end:{user_id}")],
            # In this submenu, Back should return to Settings menu, not main actions
            [InlineKeyboardButton("🔙 Back", callback_data=f"back_to_settings:{user_id}")]
        ])
        await query.message.edit_text(
            "📍 Text Position\n\n"
            f"Current: <b>{current.capitalize()}</b>\n\n"
            "Examples:\n"
            "• Start: <code>@tag Document.pdf</code>\n"
            "• End: <code>Document @tag.pdf</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"cb_change_position error: {e}")

@app.on_callback_query(filters.regex(r"^add_hashtag$"))
async def cb_add_hashtag(client, query: CallbackQuery):
    try:
        user_id = query.from_user.id
        # Set state to await username/hashtag input
        session = ensure_session_dict(user_id)
        session['state'] = UserState.AWAITING_USERNAME.value
        session['state_data'] = {}
        # Backward compatibility with existing text handler
        session['awaiting_username'] = True
        await query.message.edit_text(
            "🔖 Send a personalized keyword or @username to save as your tag (e.g., <code>@MyTag</code>).",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"cb_add_hashtag error: {e}")

@app.on_callback_query(filters.regex(r"^settings$"))
async def cb_settings(client, query: CallbackQuery):
    """Open the Settings/Parameters menu from the global Settings button."""
    try:
        user_id = query.from_user.id
        kb = build_settings_keyboard(user_id)
        # Show current hashtag (if any) and position like renambot
        saved = get_saved_username(user_id)
        current = saved or sessions.get(user_id, {}).get('username', '')
        pos = get_text_position(user_id)
        if current:
            text = (
                "⚙️ Settings\n\n"
                f"📝 Hashtag: <code>{current}</code>\n"
                f"📍 Position: {pos}\n\n"
                "Choose an option:"
            )
        else:
            text = (
                "⚙️ Settings\n\n"
                "📝 No hashtag set\n"
                f"📍 Position: {pos}\n\n"
                "Choose an option:"
            )
        await query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"cb_settings error: {e}")

@app.on_callback_query(filters.regex(r"^set_position_(start|end):(\d+)$"))
async def cb_set_position(client, query: CallbackQuery):
    try:
        data = query.data
        pos = 'start' if 'start' in data else 'end'
        user_id = query.from_user.id
        set_text_position(user_id, pos)
        await query.answer(f"Position set to {pos}")
        # Refresh position menu
        await cb_change_position(client, query)
    except Exception as e:
        logger.error(f"cb_set_position error: {e}")

@app.on_callback_query(filters.regex(r"^back_settings:(\d+)$"))
async def cb_back_settings(client, query: CallbackQuery):
    try:
        user_id = query.from_user.id
        # Return to the Start menu (same as /start)
        start_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📦 Sequence Mode", callback_data="batch_mode")]
        ])
        await query.message.edit_text(MESSAGES['start'], reply_markup=start_kb)
    except Exception as e:
        logger.error(f"cb_back_settings error: {e}")

# New: Back to Settings from a Settings submenu (e.g., Change Position)
@app.on_callback_query(filters.regex(r"^back_to_settings:(\d+)$"))
async def cb_back_to_settings(client, query: CallbackQuery):
    try:
        # Simply reopen the Settings menu
        await cb_settings(client, query)
    except Exception as e:
        logger.error(f"cb_back_to_settings error: {e}")

@app.on_message(filters.document & filters.private)
async def handle_document(client, message: Message):
    user_id = message.from_user.id
    track_user(user_id)
    
    # S'assurer que la session existe et mettre à jour l'activité
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()
    
    # NEW: Ignore if it's the bot sending
    if message.from_user.is_bot:
        logger.info(f"Document ignored - sent by the bot itself")
        return
    
    # 🔥 BATCH CORRECTION: Don't block in batch mode
    if not session.get('batch_mode'):
        # NEW: Ignore if we just processed a file (NORMAL MODE ONLY)
        if session.get('just_processed'):
            logger.info(f"Document ignored - file was just processed")
            session['just_processed'] = False
            return
        
        # Check if we're already processing something (NORMAL MODE ONLY)
        if session.get('processing'):
            logger.info(f"Document ignored - processing in progress for user {user_id}")
            return
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon et rate limit - AJUSTÉE POUR BATCH
    if not session.get('batch_mode'):
        duplicate_check = is_duplicate_message(user_id, message.id, "document")
        if duplicate_check:
            if duplicate_check == "rate_limit":
                await send_limit_message(client, message.chat.id, "rate_limit")
            elif duplicate_check == "duplicate":
                await send_limit_message(client, message.chat.id, "duplicate")
            logger.info(f"Document ignored - {duplicate_check} for user {user_id}")
            return
    
    doc = message.document
    if not doc:
        return
    
    # Vérifier si c'est une vidéo (par MIME type)
    if doc.mime_type and doc.mime_type.startswith("video/"):
        # Traiter comme une vidéo
        await handle_video_document(client, message, doc)
        return
    
    # Vérifier si c'est un PDF
    if doc.mime_type != "application/pdf" and not doc.file_name.lower().endswith('.pdf'):
        await client.send_message(message.chat.id, "❌ This file is not supported!\n\nOnly PDF and video files are accepted.")
        return
    
    # Le reste du code existant pour les PDF...
    if doc.file_size > MAX_FILE_SIZE:
        await client.send_message(message.chat.id, MESSAGES['file_too_big'])
        return
    
    file_id = doc.file_id
    file_name = doc.file_name or "document.pdf"
    
    # Vérifier si on est en mode batch
    if session.get('batch_mode'):
        # Vérifier l'existence de user_batches[user_id]
        if user_id not in user_batches:
            user_batches[user_id] = []
            
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await client.send_message(message.chat.id, f"❌ Limit of {MAX_BATCH_FILES} files reached!")
            return
        
        # 📄 AJOUTER UNIQUEMENT LES INFOS, PAS DE TÉLÉCHARGEMENT
        user_batches[user_id].append({
            'file_id': file_id,
            'file_name': file_name,
            'is_video': False,
            'message_id': message.id,
            'size': doc.file_size
        })
        
        await client.send_message(
            message.chat.id,
            f"✅ **File added to batch** ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"📄 {file_name}\n"
            f"📦 Size: {doc.file_size} bytes\n\n"
            f"Send `/process` when you're done adding files"
        )
        
        # 🔥 LOG pour debug
        logger.info(f"📦 BATCH: File added for user {user_id} - Total: {len(user_batches[user_id])}")
        return
    
    # Normal mode - create a new session
    # NEW: Load username from persistent file
    saved_username = get_saved_username(user_id)
    delete_delay = session.get('delete_delay', AUTO_DELETE_DELAY)
    
    session.update({
        'file_id': file_id,
        'file_name': file_name,
        'last_activity': datetime.now()
    })
    
    # NEW: Restore username from file
    if saved_username:
        session['username'] = saved_username
    if delete_delay != AUTO_DELETE_DELAY:
        session['delete_delay'] = delete_delay
    
    # Créer le menu selon le type de fichier
    if is_pdf_file(file_name):
        # Menu complet pour les PDF
        keyboard = build_pdf_actions_keyboard(user_id)
        message_text = f"📄 PDF received: {file_name}\n\nWhat do you want to do?"
    else:
        # Pas de menu pour les vidéos - elles sont gérées par handle_video_document
        return
    
    await client.send_message(
        message.chat.id,
        message_text,
        reply_markup=keyboard
    )

# ====== NOUVEAU HANDLER POUR LES VIDÉOS ======
@app.on_message(filters.video & filters.private)
async def handle_video(client, message: Message):
    user_id = message.from_user.id
    track_user(user_id)
    
    # S'assurer que la session existe et mettre à jour l'activité
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon et rate limit
    duplicate_check = is_duplicate_message(user_id, message.id, "video")
    if duplicate_check:
        if duplicate_check == "rate_limit":
            await send_limit_message(client, message.chat.id, "rate_limit")
        elif duplicate_check == "duplicate":
            await send_limit_message(client, message.chat.id, "duplicate")
        logger.info(f"Video ignored - {duplicate_check} for user {user_id}")
        return
    
    # Initialiser la session si nécessaire
    if user_id not in sessions:
        sessions[user_id] = {}
    
    # Sauvegarder les infos de la vidéo
    session['video_file_id'] = message.video.file_id
    session['video_file_name'] = message.video.file_name or "video.mp4"
    session['video_message_id'] = message.id
    session['last_activity'] = datetime.now()
    
    logger.info(f"🎥 Video received from user {user_id}: {session['video_file_name']}")
    
    # Créer le menu selon le mode
    if session.get('batch_mode'):
        # Mode batch - PAS DE TÉLÉCHARGEMENT, juste stocker les infos
        if user_id not in user_batches:
            user_batches[user_id] = []
            
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await client.send_message(message.chat.id, f"❌ Limit of {MAX_BATCH_FILES} files reached!")
            return
        
        # Ajouter au batch SANS téléchargement
        user_batches[user_id].append({
            'file_id': message.video.file_id,
            'file_name': message.video.file_name or "video.mp4",
            'is_video': True,
            'message_id': message.id,
            'caption': message.caption or "",  # Caption originale
            'duration': message.video.duration,
            'size': message.video.file_size
        })
        
        # Ajout vidéo en batch (sans boutons)
        await client.send_message(
            message.chat.id,
            f"✅ Video added to batch ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"What do you want to do with this video?"
        )
    else:
        # Normal mode - only Edit Name + Cancel
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Name", callback_data=f"video_edit_name:{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
            [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}")],
            [InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
        ])
    
    await client.send_message(
        message.chat.id,
            f"🎥 **Video received**: {session['video_file_name']}\n\nWhat do you want to do?",
        reply_markup=keyboard
    )


async def handle_video_document(client, message: Message, doc):
    """Processes videos sent as documents"""
    user_id = message.from_user.id
    
    # Ensure session exists and update activity
    session = ensure_session_dict(user_id)
    session['last_activity'] = datetime.now()
    
    # Save video information
    session['video_file_id'] = doc.file_id
    session['video_file_name'] = doc.file_name or "video.mp4"
    session['video_message_id'] = message.id
    session['last_activity'] = datetime.now()
    
    logger.info(f"🎥 Video document received from user {user_id}: {session['video_file_name']}")
    
    # Create menu according to mode
    if session.get('batch_mode'):
        # Batch mode - NO DOWNLOAD, just store information
        if user_id not in user_batches:
            user_batches[user_id] = []
            
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await client.send_message(message.chat.id, f"❌ Limit of {MAX_BATCH_FILES} files reached!")
            return
        
        # Add to batch WITHOUT download
        user_batches[user_id].append({
            'file_id': doc.file_id,
            'file_name': doc.file_name or "video.mp4",
            'is_video': True,
            'message_id': message.id,
            'caption': message.caption or "",  # Original caption
            'size': doc.file_size
        })
        
        # Video added to batch (no buttons)
        await client.send_message(
            message.chat.id,
            f"✅ Video added to batch ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"What do you want to do with this video?"
        )
    else:
        # Normal mode - only Edit Name + Cancel
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Name", callback_data=f"video_edit_name:{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
            [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}")],
            [InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
        ])
        
        await client.send_message(
            message.chat.id,
            f"🎥 **Video received**: {session['video_file_name']}\n\nWhat do you want to do?",
            reply_markup=keyboard
        )


# 🔥 HANDLER POUR LE BOUTON "I have joined" 🔥
@app.on_callback_query(filters.regex("^check_joined$"))
async def check_joined_handler(client, query: CallbackQuery):
    user_id = query.from_user.id
    logger.info(f"🔍 check_joined_handler called for user {user_id}")
    
    is_member = await is_user_in_channel(user_id)
    logger.info(f"🔍 Membership verification result for user {user_id}: {is_member}")
    
    if is_member:
        await query.answer("✅ Thank you! You can now use the bot.", show_alert=True)
        await query.message.delete()
        # Show welcome message
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📦 Sequence Mode", callback_data="batch_mode")]
        ])
        await client.send_message(user_id, MESSAGES['start'], reply_markup=keyboard)
        logger.info(f"✅ User {user_id} successfully verified and welcome message sent")
    else:
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        logger.info(f"❌ User {user_id} verification failed - not member of channel")

# 🔥 HANDLERS POUR LES BOUTONS DE SUPPRESSION DE PAGES 🔥
@app.on_callback_query(filters.regex(r"^the_first:(\d+)$"))
async def remove_first_page(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    await remove_page_logic(client, query, user_id, page_number=1)

@app.on_callback_query(filters.regex(r"^the_last:(\d+)$"))
async def remove_last_page(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        await query.edit_message_text("❌ Aucun fichier PDF trouvé.")
        return
    
    # Calculate the last page automatically
    file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
    try:
        with pikepdf.open(file_path) as pdf:
            last_page = len(pdf.pages)
        await remove_page_logic(client, query, user_id, page_number=last_page)
    except pikepdf.PasswordError:
        await query.edit_message_text("❌ Cannot delete page (PDF protected)")

@app.on_callback_query(filters.regex(r"^the_middle:(\d+)$"))
async def remove_middle_page(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        await query.edit_message_text("❌ No PDF file found.")
        return
    
    # Calculate the middle page
    file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
    try:
        with pikepdf.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            middle_page = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
        await remove_page_logic(client, query, user_id, page_number=middle_page)
    except pikepdf.PasswordError:
        await query.edit_message_text("❌ Cannot delete page (PDF protected)")

@app.on_callback_query(filters.regex(r"^enter_manually:(\d+)$"))
async def ask_user_page_input(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    session = ensure_session_dict(user_id)
    session['awaiting_page_number'] = True
    await query.edit_message_text("📝 Enter the page number to delete:")


# ====== CALLBACKS POUR LES VIDÉOS ======
@app.on_callback_query(filters.regex(r"^video_clean_name:(\d+)$"))
async def video_clean_name_callback(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    
    await query.answer("✅ Cleaning video name...", show_alert=False)
    
    # Récupérer la vidéo dans le batch
    video_entry = None
    for entry in user_batches.get(user_id, []):
        if entry.get('is_video'):
            video_entry = entry
            break
    
    if not video_entry:
        await query.edit_message_text("❌ Video not found in batch.")
        return
    
    file_id = video_entry['file_id']
    original_caption = video_entry.get('caption', '')
    
    # Nettoyer la caption et ajouter le username
    final_caption = clean_caption_with_username(original_caption, user_id)
    
    try:
        # Renvoyer la vidéo avec la caption nettoyée (SANS téléchargement)
        await client.send_video(
            chat_id=query.message.chat.id,
            video=file_id,  # Utilise le file_id original
            caption=final_caption
        )
        
        # Supprimer le message du menu
        try:
            await query.message.delete()
        except:
            pass
        
        # Envoyer le message de succès
        await client.send_message(
            query.message.chat.id,
            "✅ Video cleaned and sent successfully!"
        )
        
        logger.info(f"✅ Video sent with cleaned caption: {final_caption}")
        
    except Exception as e:
        logger.error(f"Error in video_clean_name: {e}")
        await query.edit_message_text(f"❌ Error: {str(e)}")


@app.on_callback_query(filters.regex(r"^video_edit_name:(\d+)$"))
async def video_edit_name_callback(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    
    if query.from_user.id != user_id:
        await query.answer("❌ This is not for you!", show_alert=True)
        return
    
    await query.answer()
    
    session = ensure_session_dict(user_id)
    video_file_id = session.get('video_file_id')
    
    if not video_file_id:
        await query.edit_message_text("❌ No video in session. Send a video first.")
        return
    
    # Activer le mode d'attente du nouveau nom
    session['awaiting_video_name'] = True
    session['video_edit_message_id'] = query.message.id
    
    await query.edit_message_text(
        "✏️ **Send me the new filename for the video**\n\n"
        "Example: `My Amazing Video.mp4`"
    )


# ====== CALLBACK GÉNÉRIQUE POUR NETTOYER LES NOMS ======
@app.on_callback_query(filters.regex(r"^clean_name:(.+)$"))
async def clean_name_callback(client, query: CallbackQuery):
    try:
        file_id = query.data.split(":")[1]
        msg = await client.get_messages(chat_id=query.message.chat.id, message_ids=int(file_id))

        if not msg.video and not msg.document:
            return await query.answer("❌ This button only works for video and document files.", show_alert=True)

        await query.answer("✅ Cleaning name...", show_alert=False)

        if msg.video:
            # Traitement vidéo - utiliser la fonction unifiée
            original_caption = msg.caption or ""
            success = await clean_and_send_video(
                client=client,
                chat_id=query.message.chat.id,
                file_id=msg.video.file_id,
                caption=original_caption,
                user_id=query.from_user.id
            )
            
            if not success:
                await query.answer("❌ Error processing video", show_alert=True)
                return
        elif msg.document:
            # Traitement document (PDF) - utiliser la caption originale
            original_caption = msg.caption or ""
            final_caption = clean_caption_with_username(original_caption, query.from_user.id)

            await client.send_document(
                chat_id=query.message.chat.id,
                document=msg.document.file_id,
                caption=final_caption
            )

        await query.message.reply_text("✅ File name cleaned successfully!")

    except Exception as e:
        logger.exception("❌ Error in clean_name_callback:")
        await query.answer("⚠️ An error occurred while cleaning the name.", show_alert=True)

# 🔥 FONCTION DE LOGIQUE DE SUPPRESSION AVEC VÉRIFICATION DE VERROUILLAGE 🔥
async def remove_page_logic(client, origin, user_id, page_number):
    """Common logic for deleting a page"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        # Gracefully handle both CallbackQuery and Message
        try:
            await origin.edit_message_text("❌ No PDF file found.")
        except Exception:
            try:
                chat_id = origin.chat.id
            except Exception:
                chat_id = None
            if chat_id:
                await client.send_message(chat_id, "❌ No PDF file found.")
        return
    
    file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
    
    # Check if PDF is locked
    try:
        with pikepdf.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            if page_number < 1 or page_number > total_pages:
                try:
                    await origin.edit_message_text(f"❌ Invalid page number. The PDF has {total_pages} pages.")
                except Exception:
                    try:
                        chat_id = origin.chat.id
                    except Exception:
                        chat_id = None
                    if chat_id:
                        await client.send_message(chat_id, f"❌ Invalid page number. The PDF has {total_pages} pages.")
                return
            
            # Delete the page
            del pdf.pages[page_number - 1]
            
            # Save the modified PDF
            output_path = f"{get_user_temp_dir(user_id)}/modified_{session.get('file_name', 'document.pdf')}"
            pdf.save(output_path)
            
            # Send the modified file directly (no confirmation message)
            username = session.get('username', '')
            new_file_name = build_final_filename(user_id, session.get('file_name', 'document.pdf'))
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            
            # Determine chat id from origin (CallbackQuery or Message)
            try:
                chat_id = origin.message.chat.id
            except Exception:
                chat_id = origin.chat.id
            await send_and_delete(client, chat_id, output_path, new_file_name, delay_seconds=delay)
            try:
                await origin.edit_message_text(f"✅ Page {page_number} deleted successfully!")
            except Exception:
                # Fallback to sending a new message
                await client.send_message(chat_id, f"✅ Page {page_number} deleted successfully!")
            
            # ✅ FIX: Reset processing flag after successful page removal
            clear_processing_flag(user_id, source="remove_page", reason="completed")
    except pikepdf.PasswordError:
        try:
            await origin.edit_message_text("❌ Cannot delete page (PDF protected)")
        except Exception:
            try:
                chat_id = origin.chat.id
            except Exception:
                chat_id = None
            if chat_id:
                await client.send_message(chat_id, "❌ Cannot delete page (PDF protected)")

@app.on_callback_query() 
async def button_callback(client, query: CallbackQuery):
    # Debug logging
    logger.info(f"DEBUG callback_query data: {query.data}")
    
    if query.data == "check_joined":
        return  # Already handled by specific handler
    
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    # S'assurer que la session existe
    session = ensure_session_dict(user_id)
    
    # Reset tous les flags temporaires au début
    reset_session_flags(user_id)
    
    # Check if action is already in progress, but allow UI-only callbacks (prompts) to go through
    if (
        sessions[user_id].get('processing')
        and not data.startswith("clean_username")
        and not data.startswith("delay_")
        and not data.startswith("full_")
        and not data.startswith("batch_fullproc")
        and not data.startswith("batch_pages")
        and not data.startswith("batch_unlock")
        and not data.startswith("batch_both")
        and not data.startswith("batch_lock")
        and data not in ["settings", "set_delete_delay", "back_to_start"]
    ):
        await query.answer("⏳ Processing already in progress...", show_alert=True)
        return
    
    # Don't mark processing=True for clean_username, settings, delay, full_* selectors, and batch UI prompts
    if (
        not data.startswith("clean_username")
        and not data.startswith("cancel")
        and not data.startswith("delay_")
        and not data.startswith("full_")
        and not data.startswith("batch_fullproc")
        and not data.startswith("batch_pages")
        and not data.startswith("batch_unlock")
        and not data.startswith("batch_both")
        and not data.startswith("batch_lock")
        and not data.startswith("edit_")
        and data not in ["settings", "set_delete_delay", "back_to_start"]
    ):
        set_processing_flag(user_id, chat_id=query.message.chat.id, source=f"cb:{data}")
    
    # Batch mode handling
    if data == "batch_mode":
        sessions[user_id]['batch_mode'] = True
        
        await query.edit_message_text(
            f"📦 **Sequence Mode Activated**\n\n"
            f"✅ You can now send up to {MAX_BATCH_FILES} files\n"
            f"🎥 Videos will be downloaded immediately to avoid session expiration\n"
            f"📄 PDFs will be processed when you send `/process`\n\n"
            f"⏰ **Important**: Videos must be processed within 1-2 minutes of sending\n"
            f"🔄 Send `/process` when you're done adding files\n\n"
            f"To disable batch mode, send `/start`"
        )
        clear_processing_flag(user_id, source="batch_mode", reason="menu_shown")
        return
    
    # Batch clear handling
    elif data.startswith("batch_clear:"):
        user_id = int(data.split(":")[1])
        if user_id not in user_batches:
            user_batches[user_id] = []
        clear_user_batch(user_id)
        await query.edit_message_text("🧹 Batch cleared successfully!")
        clear_processing_flag(user_id, source="batch_clear", reason="done")
        return
    
    # Batch actions handling - FIXED SECTION
    elif data.startswith("batch_"):
        parts = data.split(":")
        action = parts[0]
        user_id = int(parts[1]) if len(parts) > 1 else query.from_user.id
        
        # Ensure user session exists
        if user_id not in sessions:
            sessions[user_id] = {}
        
        # Ensure batch exists
        if user_id not in user_batches:
            user_batches[user_id] = []
        
        if action == "batch_clean":
            await process_batch_clean(client, query.message, user_id)
            return
        
        elif action == "batch_unlock":
            sessions[user_id]['batch_action'] = 'unlock'
            sessions[user_id]['awaiting_batch_password'] = True
            await query.edit_message_text("🔐 Send me the password for all PDFs:")
            # Do not keep processing active while waiting for user input
            clear_processing_flag(user_id, source="batch_unlock", reason="awaiting_input")
            return
        
        elif action == "batch_pages":
            sessions[user_id]['batch_action'] = 'pages'
            await query.edit_message_text(
                "📝 Which pages to remove from all files?\n\n"
                "Examples:\n"
                "• 1 → removes page 1\n"
                "• 1,3,5 → removes pages 1, 3 and 5\n"
                "• 1-5 → removes pages 1 to 5"
            )
            # Do not keep processing active while waiting for user input
            clear_processing_flag(user_id, source="batch_pages", reason="awaiting_input")
            return
        
        elif action == "batch_both":
            sessions[user_id]['batch_action'] = 'both'
            sessions[user_id]['awaiting_batch_both_password'] = True
            await safe_edit_message(query,
                "🛠️ **The Both - Batch**\n\n"
                "This function will:\n"
                "1. Unlock the PDF (if protected)\n"
                "2. Remove selected pages\n"
                "3. Clean @username and hashtags\n"
                "4. Add your custom username\n\n"
                "**Step 1/2:** Send me the password (or 'none' if not protected):"
            )
            # Do not keep processing active while waiting for user input
            clear_processing_flag(user_id, source="batch_both", reason="awaiting_input")
            return
        
        elif action == "batch_add_banner":
            sessions[user_id]['batch_action'] = 'add_banner'
            await process_batch_add_banner(client, query.message, user_id)
            return
        
        elif action == "batch_lock":
            # Auto-use saved default lock password if any; otherwise proceed without lock (no prompt)
            sessions[user_id]['batch_action'] = 'lock'
            pw = (get_user_pdf_settings(user_id) or {}).get('lock_password') or ''
            if not pw:
                try:
                    await query.edit_message_text("ℹ️ No default lock password — proceeding without lock for all PDFs.")
                except Exception:
                    pass
            await process_batch_lock(client, query.message, user_id, pw)
            return
        # FIXED: Added batch_both_first, batch_both_last, batch_both_middle, batch_both_manual handlers
        elif action == "batch_both_first":
            logger.info(f"🔍 DEBUG: batch_both_first called for user {user_id}")
            
            # Vérifier l'état de la session
            if user_id not in sessions:
                logger.error(f"❌ DEBUG: No session found for user {user_id}")
            else:
                logger.info(f"🔍 DEBUG: Session exists for user {user_id}")
                logger.info(f"🔍 DEBUG: Session keys: {list(sessions[user_id].keys())}")
            
            # Utiliser ensure_session_dict pour garantir l'existence
            session = ensure_session_dict(user_id)
            password = session.get('batch_both_password', '')
            
            logger.info(f"🔑 DEBUG: Password retrieved: {bool(password)}")
            logger.info(f"🔑 DEBUG: Password length: {len(password) if password else 0}")
            
            if password:
                await process_batch_both(client, query.message, user_id, password, "1")
                return
            else:
                logger.error(f"❌ batch_both_first - No password found for user {user_id}")
                logger.error(f"❌ DEBUG: Session content: {session}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
                return
        
        elif action == "batch_both_last":
            # FIX: Utiliser ensure_session_dict
            session = ensure_session_dict(user_id)
            password = session.get('batch_both_password', '')
            
            if password:
                files = user_batches.get(user_id, [])
                if files:
                    file = await client.download_media(files[0]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
                    try:
                        with pikepdf.open(file) as pdf:
                            total_pages = len(pdf.pages)
                        os.remove(file)
                        await process_batch_both(client, query.message, user_id, password, str(total_pages))
                        return
                    except Exception as e:
                        logger.error(f"Error getting last page: {e}")
                        await safe_edit_message(query, "❌ Error reading PDF")
                        return
                else:
                    await safe_edit_message(query, "❌ No files in batch")
                    return
            else:
                logger.error(f"❌ batch_both_last - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
                return
        
        elif action == "batch_both_middle":
            # FIX: Utiliser ensure_session_dict
            session = ensure_session_dict(user_id)
            password = session.get('batch_both_password', '')
            
            if password:
                files = user_batches.get(user_id, [])
                if files:
                    file = await client.download_media(files[0]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
                    try:
                        with pikepdf.open(file) as pdf:
                            total_pages = len(pdf.pages)
                        middle = total_pages // 2
                        os.remove(file)
                        await process_batch_both(client, query.message, user_id, password, str(middle))
                        return
                    except Exception as e:
                        logger.error(f"Error getting middle page: {e}")
                        await safe_edit_message(query, "❌ Error reading PDF")
                        return
                else:
                    await safe_edit_message(query, "❌ No files in batch")
                    return
            else:
                logger.error(f"❌ batch_both_middle - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
                return
        
        elif action == "batch_both_manual":
            # FIX: Utiliser ensure_session_dict et vérifier le password
            session = ensure_session_dict(user_id)
            password = session.get('batch_both_password', '')
            
            if password:
                sessions[user_id]['awaiting_batch_both_pages'] = True
                await safe_edit_message(query,
                    "📝 **Manual page entry - Batch**\n\n"
                    "Send me the pages to remove:\n"
                    "• 1 → removes page 1\n"
                    "• 1,3,5 → removes pages 1, 3 and 5\n"
                    "• 1-5 → removes pages 1 to 5"
                )
                # Do not keep processing active while waiting for user input
                clear_processing_flag(user_id, source="batch_both_manual", reason="awaiting_input")
                return
            else:
                logger.error(f"❌ batch_both_manual - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to process again.")
                return
    
    # Gestion des paramètres
    if data == "settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add a #htag", callback_data="add_username")],
            [InlineKeyboardButton("⏰ Delete delay", callback_data="set_delete_delay")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]
        ])
        await query.edit_message_text(
            "⚙️ **Settings Panel**\n\n"
            "Configure the bot according to your needs.",
            reply_markup=keyboard
        )
        clear_processing_flag(user_id, source="settings", reason="menu_shown")
        return
    
    elif data == "set_delete_delay":
        logger.info(f"🔍 Set delete delay callback received for user {user_id}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 minute", callback_data="delay_60")],
            [InlineKeyboardButton("5 minutes", callback_data="delay_300")],
            [InlineKeyboardButton("10 minutes", callback_data="delay_600")],
            [InlineKeyboardButton("30 minutes", callback_data="delay_1800")],
            [InlineKeyboardButton("Never", callback_data="delay_0")],
            [InlineKeyboardButton("🔙 Back", callback_data="settings")]
        ])
        await query.edit_message_text(
            "⏰ **Auto-delete delay**\n\n"
            "After how long should files be deleted?",
            reply_markup=keyboard
        )
        return
    
    elif data.startswith("delay_"):
        logger.info(f"🔍 Delay callback received: {data}")
        delay = int(data.split("_")[1])
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['delete_delay'] = delay
        
        logger.info(f"🔍 Setting delay to {delay} seconds for user {user_id}")
        
        if delay == 0:
            await query.edit_message_text("✅ Auto-delete disabled")
        else:
            await query.edit_message_text(f"✅ Files will be deleted after {delay//60} minute(s)")
        return
    
    elif data == "add_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        sessions[user_id]['awaiting_username'] = True
        await query.edit_message_text(
            "Send me the text/tag to add to your files.\n\n"
            "You can send:\n"
            "• @username\n"
            "• #hashtag\n"
            "• [📢 @channel]\n"
            "• 🔥 @fire\n"
            "• Any text with emojis!"
        )
        logger.info(f"🔍 Username addition mode activated for user {user_id}")
        return
    
    elif data == "delete_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        logger.info(f"🔍 Delete username - User {user_id} - Existing username: {sessions[user_id].get('username')}")
        
        # 🔥 Correction here: remove 'username' key from session AND persistent file
        if sessions[user_id].get('username'):
            old_username = sessions[user_id]['username']
            sessions[user_id].pop('username', None)  # <-- properly pop the key here
            
            # Also remove from persistent file
            delete_saved_username(user_id)
            
            await query.edit_message_text(f"✅ Username deleted: {old_username}")
            logger.info(f"🔍 Username deleted for user {user_id}: {old_username}")
        else:
            await query.edit_message_text("ℹ️ No username registered.")
            logger.info(f"🔍 No username to delete for user {user_id}")
        return
    
    elif data == "back_to_start":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📦 Sequence Mode", callback_data="batch_mode")]
        ])
        await query.edit_message_text(MESSAGES['start'], reply_markup=keyboard)
        return
    
    # Gestion des actions PDF
    if ":" not in data:
        clear_processing_flag(user_id, source="no_action", reason="no_colon")
        return
    
    action, user_id = data.split(":")
    user_id = int(user_id)
    
    # CORRECTION 1: Pour clean_username, marquer un flag spécial
    if action == "clean_username":
        if user_id not in sessions:
            sessions[user_id] = {}
        sessions[user_id]['cleaning_only'] = True
        # S'assurer que la session contient les données nécessaires
        if 'file_id' not in sessions[user_id]:
            await query.edit_message_text("❌ No file in session. Send a PDF first.")
            clear_processing_flag(user_id, source="clean_username", reason="no_file")
            return
        await process_clean_username(client, query.message, sessions[user_id])
        return
    if action == "cancel":
        sessions.pop(user_id, None)
        await query.edit_message_text("❌ Operation cancelled")
        return
    
    if user_id not in sessions:
        await query.edit_message_text("❌ Session expired. Send the PDF again.")
        clear_processing_flag(user_id, source="expired", reason="no_session")
        return
    
    sessions[user_id]['action'] = action
    
    if action == "unlock":
        await query.edit_message_text("🔐 Send me the PDF password:")
    elif action == "pages":
        page_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("The First", callback_data=f"the_first:{user_id}"),
                InlineKeyboardButton("The Last", callback_data=f"the_last:{user_id}"),
                InlineKeyboardButton("The Middle", callback_data=f"the_middle:{user_id}")
            ],
            [InlineKeyboardButton("📝 Enter manually", callback_data=f"enter_manually:{user_id}")]
        ])
        await query.edit_message_text(
            "Which pages do you want to remove?\n\n"
            "Examples:\n"
            "• 1 → removes page 1\n"
            "• 1,3,5 → removes pages 1, 3 and 5\n"
            "• 1-5 → removes pages 1 to 5",
            reply_markup=page_buttons
        )
        clear_processing_flag(user_id, source="remove_pages_menu", reason="menu_shown")  # Libérer le flag pour les boutons
    elif action == "both":
        # Show options for "The Both" action
        both_options = InlineKeyboardMarkup([
            [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}"), InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
            [InlineKeyboardButton("Remove/Unlock", callback_data=f"both_full:{user_id}")],
            [InlineKeyboardButton("🗑️ Remove Pages Only", callback_data=f"both_remove_pages:{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
        ])
        await client.send_message(
            query.message.chat.id,
            "🛠️ **The Both** - Combined action\n\n"
            "This function will:\n"
            "1. Unlock the PDF (if protected)\n"
            "2. Clean @username and hashtags\n"
            "3. Add your custom username\n\n"
            "What do you want to do?",
            reply_markup=both_options
        )
        clear_processing_flag(user_id, source="both_menu", reason="menu_shown")  # Libérer le flag pour les boutons
        return
    elif action == "both_full":
        sessions[user_id]['awaiting_both_password'] = True
        await safe_edit_message(query,
            "🛠️ **The Both** - Combined action\n\n"
            "This function will:\n"
            "1. Unlock the PDF (if protected)\n"
            "2. Remove selected pages\n"
            "3. Clean @username and hashtags\n"
            "4. Add your custom username\n\n"
            "**Step 1/2:** Send me the PDF password (or 'none' if not protected):"
        )
    elif action == "both_remove_pages":
        # Show the remove pages menu for "both" action
        await query.edit_message_text(
            "🗑️ **Remove Pages** - The Both\n\n"
            "Choose which page to remove:",
            reply_markup=get_remove_pages_buttons(user_id)
        )
        clear_processing_flag(user_id, source="both_remove_pages_menu", reason="menu_shown")  # Libérer le flag pour les boutons
        return
    elif action in ("fullproc", "batch_fullproc"):
        # Start FULL PROCESS: ask for unlock password first
        logger.info(f"[fullproc_init] User %s triggered %s", user_id, action)
        sessions[user_id]['awaiting_full_password'] = True
        # Track if this is the batch mode launcher to adjust subsequent flow
        sessions[user_id]['fullproc_is_batch'] = (action == "batch_fullproc")
        await safe_edit_message(
            query,
            "⚡️ **Full Process**\n\n"
            "This will: Unlock (if needed) → Clean banners → Add banner → Remove pages → Lock → Send.\n\n"
            "Step 1/2: Send the PDF password (or 'none' if not protected)."
        )
        # Free the flag so the user can press selection buttons without being blocked
        clear_processing_flag(user_id, source="fullproc_init", reason="awaiting_unlock_pw")
        return
    elif action == "add_banner":
        # Add default banner immediately
        banner_pdf = _ensure_banner_pdf_path(user_id)
        if not banner_pdf:
            await query.edit_message_text("❌ No default banner. Use /setbanner first.")
            clear_processing_flag(user_id, source="add_banner", reason="no_default_banner")
            return
        session2 = ensure_session_dict(user_id)
        file_id2 = session2.get('file_id')
        file_name2 = session2.get('file_name') or 'document.pdf'
        user_dir2 = get_user_temp_dir(user_id)
        if not file_id2:
            await query.edit_message_text("❌ No PDF in session.")
            clear_processing_flag(user_id, source="add_banner", reason="no_pdf_in_session")
            return
        in_path2 = await client.download_media(file_id2, file_name=user_dir2 / 'banner_input.pdf')
        out_path2 = str(user_dir2 / f"bannered_{file_name2}")
        # Show processing status
        status_msg = await client.send_message(query.message.chat.id, MESSAGES['processing'])
        logger.info(f"[add_banner] Start for user %s", user_id)
        cleaned_input = in_path2
        try:
            # 1) Clean existing user banners from the input PDF (no-op if cleaner unavailable)
            try:
                logger.info("[add_banner] Cleaning banners for user %s", user_id)
                with open(in_path2, 'rb') as f:
                    raw_bytes = f.read()
                cleaned_bytes = await run_in_thread_with_timeout(
                    clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                )
                cleaned_input = str(Path(user_dir2) / 'banner_input_cleaned.pdf')
                with open(cleaned_input, 'wb') as cf:
                    cf.write(cleaned_bytes)
                logger.info("[add_banner] Cleaning complete for user %s", user_id)
            except asyncio.TimeoutError as te:
                logger.warning(f"[add_banner] Cleaning timed out for user {user_id}: {te}")
                cleaned_input = in_path2
            except Exception as ce:
                logger.warning(f"[add_banner] Cleaning failed or skipped for user {user_id}: {ce}")
                # If cleaning fails for any reason, proceed with original input
                cleaned_input = in_path2

            # 2) Add the configured banner pages at the end
            logger.info("[add_banner] Adding banner pages for user %s", user_id)
            await run_in_thread_with_timeout(
                add_banner_pages_to_pdf, cleaned_input, out_path2, banner_pdf, place='after', timeout=BANNER_ADD_TIMEOUT
            )
            delay2 = session2.get('delete_delay', AUTO_DELETE_DELAY)
            username2 = session2.get('username')
            await send_and_delete(client, query.message.chat.id, out_path2,
                                  build_final_filename(user_id, Path(out_path2).name),
                                  delay_seconds=delay2)
            # Remove processing message
            try:
                await status_msg.delete()
            except:
                pass
            await query.edit_message_text("✅ Banner added successfully!")
        except Exception as e:
            try:
                await status_msg.delete()
            except:
                pass
            await query.edit_message_text(f"❌ Error adding banner: {e}")
        finally:
            # Always clear processing flag and cleanup temporary cleaned file if created
            clear_processing_flag(user_id, source="add_banner", reason="completed")
            if cleaned_input and cleaned_input != in_path2:
                try:
                    os.remove(cleaned_input)
                except Exception:
                    pass
            logger.info(f"[add_banner] Done for user %s (processing cleared)", user_id)
        return
    elif action == "lock_now":
        # Lock current PDF using default password
        password = get_user_pdf_settings(user_id).get('lock_password')
        if not password:
            # Proceed without locking: download and send the original file
            await query.edit_message_text("ℹ️ No default password set — sending without lock.")
            session3 = ensure_session_dict(user_id)
            file_id3 = session3.get('file_id')
            file_name3 = session3.get('file_name') or 'document.pdf'
            user_dir3 = get_user_temp_dir(user_id)
            in_path3 = await client.download_media(file_id3, file_name=user_dir3 / 'lock_input.pdf')
            status_msg = await client.send_message(query.message.chat.id, MESSAGES['processing'])
            try:
                delay3 = session3.get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(
                    client,
                    query.message.chat.id,
                    in_path3,
                    build_final_filename(user_id, file_name3),
                    delay_seconds=delay3,
                )
                try:
                    await status_msg.delete()
                except:
                    pass
                await query.edit_message_text("✅ Sent without lock.")
                # Clear any processing/session flags now that we're done
                try:
                    clear_processing_flag(user_id, source="lock_now", reason="sent_without_lock")
                except Exception:
                    pass
            except Exception as e:
                try:
                    await status_msg.delete()
                except:
                    pass
                await query.edit_message_text(f"❌ Error sending PDF: {e}")
            return
        session3 = ensure_session_dict(user_id)
        file_id3 = session3.get('file_id')
        file_name3 = session3.get('file_name') or 'document.pdf'
        user_dir3 = get_user_temp_dir(user_id)
        in_path3 = await client.download_media(file_id3, file_name=user_dir3 / 'lock_input.pdf')
        out_path3 = str(user_dir3 / f"locked_{file_name3}")
        # Show processing status
        status_msg = await client.send_message(query.message.chat.id, MESSAGES['processing'])
        try:
            lock_pdf_with_password(in_path3, out_path3, password)
            delay3 = session3.get('delete_delay', AUTO_DELETE_DELAY)
            username3 = session3.get('username')
            await send_and_delete(client, query.message.chat.id, out_path3,
                                  build_final_filename(user_id, Path(out_path3).name),
                                  delay_seconds=delay3)
            # Remove processing message
            try:
                await status_msg.delete()
            except:
                pass
            await query.edit_message_text("✅ PDF locked successfully!")
        except pikepdf.PasswordError:
            try:
                await status_msg.delete()
            except:
                pass
            await query.edit_message_text("❌ The PDF is locked. Please unlock it first, then try again.")
            clear_processing_flag(user_id, source="lock_now", reason="pdf_locked")
            return
        except Exception as e:
            try:
                await status_msg.delete()
            except:
                pass
            await query.edit_message_text(f"❌ Error locking PDF: {e}")
        return

    # --- NEW: Handlers for both_first, both_last, both_middle, both_manual ---
    if data.startswith("both_first:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        file_id = session.get('file_id')
        if not file_id:
            await query.answer("❌ No PDF in session.", show_alert=True)
            return
        # Use unified logic (1-based index)
        status_msg = await client.send_message(query.message.chat.id, "⏳ Remove pages en cours...")
        await remove_page_logic(client, query, user_id, 1)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return
    elif data.startswith("both_last:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        file_id = session.get('file_id')
        if not file_id:
            await query.answer("❌ No PDF in session.", show_alert=True)
            return
        # Compute last page by briefly opening the PDF
        try:
            tmp_info = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_info.pdf")
            with pikepdf.open(tmp_info) as pdf:
                last_page = len(pdf.pages)
        except Exception as e:
            await query.answer("❌ Error reading PDF.", show_alert=True)
            return
        finally:
            try:
                os.remove(tmp_info)
            except Exception:
                pass
        status_msg = await client.send_message(query.message.chat.id, "⏳ Remove pages en cours...")
        await remove_page_logic(client, query, user_id, last_page)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return
    elif data.startswith("both_middle:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        file_id = session.get('file_id')
        if not file_id:
            await query.answer("❌ No PDF in session.", show_alert=True)
            return
        # Compute middle page
        try:
            tmp_info = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_info.pdf")
            with pikepdf.open(tmp_info) as pdf:
                n = len(pdf.pages)
            middle = max(1, (n + 1) // 2)
        except Exception as e:
            await query.answer("❌ Error reading PDF.", show_alert=True)
            return
        finally:
            try:
                os.remove(tmp_info)
            except Exception:
                pass
        status_msg = await client.send_message(query.message.chat.id, "⏳ Remove pages en cours...")
        await remove_page_logic(client, query, user_id, middle)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return
    elif data.startswith("both_manual:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        session["awaiting_both_manual_page"] = True
        await query.message.reply_text("✏️ Enter the page number to delete.")
        return
    elif data.startswith("both_full:"):
        user_id = int(data.split(":")[1])
        sessions[user_id]['awaiting_both_password'] = True
        await safe_edit_message(query,
            "🛠️ **The Both** - Combined action\n\n"
            "This function will:\n"
            "1. Unlock the PDF (if protected)\n"
            "2. Remove selected pages\n"
            "3. Clean @username and hashtags\n"
            "4. Add your custom username\n\n"
            "**Step 1/2:** Send me the PDF password (or 'none' if not protected):"
        )
        return
    elif data.startswith("both_remove_pages:"):
        user_id = int(data.split(":")[1])
        # Show the remove pages menu for "both" action
        await query.edit_message_text(
            "🗑️ **Remove Pages** - The Both\n\n"
            "Choose which page to remove:",
            reply_markup=get_remove_pages_buttons(user_id)
        )
        # Inform the user
        try:
            await client.send_message(query.message.chat.id, "🗑️ Remove Pages menu opened. Select an option.")
        except Exception:
            pass
        clear_processing_flag(user_id, source="batch_remove_pages_menu", reason="menu_shown")  # Libérer le flag pour les boutons batch
        return
    # ==== FULL PROCESS page selection handlers ====
    elif data.startswith("full_first:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2.pop('awaiting_both_pages', None)
        # If we are already waiting for the lock password, ignore further page clicks to avoid spam
        if sess2.get('awaiting_full_lock_password'):
            try:
                await query.answer("Waiting for lock password. Send 'skip' or a password.")
            except Exception:
                pass
            return
        logger.info("[fullproc_select] user=%s pages=first", uid)
        pw = sess2.get('full_password', '')
        # If launched from batch, process the entire batch instead of single session file
        if sess2.get('fullproc_is_batch'):
            default_pw = get_user_pdf_settings(uid).get("lock_password")
            if not default_pw:
                # No default password -> proceed without locking
                try:
                    await safe_edit_message(query, "ℹ️ No default password set — proceeding without lock.")
                except Exception:
                    pass
                await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='first', lock_pw=None)
                return
            await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='first', lock_pw=default_pw)
            return
        await run_full_pipeline_and_send(client, query.message.chat.id, uid, pw, pages_spec='first')
        return
    elif data.startswith("full_last:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2.pop('awaiting_both_pages', None)
        if sess2.get('awaiting_full_lock_password'):
            try:
                await query.answer("Waiting for lock password. Send 'skip' or a password.")
            except Exception:
                pass
            return
        logger.info("[fullproc_select] user=%s pages=last", uid)
        pw = sess2.get('full_password', '')
        if sess2.get('fullproc_is_batch'):
            default_pw = get_user_pdf_settings(uid).get("lock_password")
            if not default_pw:
                try:
                    await safe_edit_message(query, "ℹ️ No default password set — proceeding without lock.")
                except Exception:
                    pass
                await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='last', lock_pw=None)
                return
            await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='last', lock_pw=default_pw)
            return
        await run_full_pipeline_and_send(client, query.message.chat.id, uid, pw, pages_spec='last')
        return
    elif data.startswith("full_middle:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2.pop('awaiting_both_pages', None)
        if sess2.get('awaiting_full_lock_password'):
            try:
                await query.answer("Waiting for lock password. Send 'skip' or a password.")
            except Exception:
                pass
            return
        logger.info("[fullproc_select] user=%s pages=middle", uid)
        pw = sess2.get('full_password', '')
        if sess2.get('fullproc_is_batch'):
            default_pw = get_user_pdf_settings(uid).get("lock_password")
            if not default_pw:
                try:
                    await safe_edit_message(query, "ℹ️ No default password set — proceeding without lock.")
                except Exception:
                    pass
                await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='middle', lock_pw=None)
                return
            await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='middle', lock_pw=default_pw)
            return
        await run_full_pipeline_and_send(client, query.message.chat.id, uid, pw, pages_spec='middle')
        return
    elif data.startswith("full_none:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2.pop('awaiting_both_pages', None)
        if sess2.get('awaiting_full_lock_password'):
            try:
                await query.answer("Waiting for lock password. Send 'skip' or a password.")
            except Exception:
                pass
            return
        logger.info("[fullproc_select] user=%s pages=none", uid)
        pw = sess2.get('full_password', '')
        # Run full pipeline keeping all pages (no deletion)
        if sess2.get('fullproc_is_batch'):
            default_pw = get_user_pdf_settings(uid).get("lock_password")
            if not default_pw:
                try:
                    await safe_edit_message(query, "ℹ️ No default password set — proceeding without lock.")
                except Exception:
                    pass
                await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='none', lock_pw=None)
                return
            await run_full_pipeline_batch_and_send(client, query.message.chat.id, uid, pw, pages_spec='none', lock_pw=default_pw)
            return
        await run_full_pipeline_and_send(client, query.message.chat.id, uid, pw, pages_spec='none')
        return
    elif data.startswith("full_manual:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2.pop('awaiting_both_pages', None)
        # If we are already waiting for the lock password, ignore switching to manual pages
        if sess2.get('awaiting_full_lock_password'):
            try:
                await query.answer("Waiting for lock password. Send 'skip' or a password.")
            except Exception:
                pass
            return
        sess2["awaiting_full_manual_pages"] = True
        logger.info("[fullproc_select] user=%s pages=manual (awaiting input)", uid)
        await query.message.reply_text("✏️ Send pages to remove (e.g. 1,3-5). Send 'none' to keep all pages.")
        return

    # ==== /pdf_edit page selection handlers ====
    elif data.startswith("edit_first:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2["awaiting_pdf_edit_pages"] = False
        await _pdf_edit_apply_pages_and_continue(client, uid, query.message.chat.id, "first")
        return
    elif data.startswith("edit_last:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2["awaiting_pdf_edit_pages"] = False
        await _pdf_edit_apply_pages_and_continue(client, uid, query.message.chat.id, "last")
        return
    elif data.startswith("edit_middle:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2["awaiting_pdf_edit_pages"] = False
        await _pdf_edit_apply_pages_and_continue(client, uid, query.message.chat.id, "middle")
        return
    elif data.startswith("edit_manual:"):
        uid = int(data.split(":")[1])
        if query.from_user.id != uid:
            await query.answer("❌ This is not for you!", show_alert=True)
            return
        sess2 = ensure_session_dict(uid)
        sess2["awaiting_pdf_edit_pages"] = True
        await query.message.reply_text("✏️ Send pages to remove (e.g. 1,3-5). Send 'none' to keep all pages.")
        return

@app.on_message(filters.text & filters.private)
async def handle_all_text(client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id, {})
    
    if user_id in sessions:
        sessions[user_id]['last_activity'] = datetime.now()

    # Short-circuit commands that might not be caught in some environments
    if message.text:
        txt = message.text.strip().lower()
        # Fallbacks for commands in case some environments miss command handlers
        if txt.startswith("/setbanied") or txt.startswith("/setbaneid"):
            await cmd_setbanied(client, message)
            return
        if txt.startswith("/setbanner"):
            await cmd_setbanner(client, message)
            return
        if txt.startswith("/view_banner") or txt.startswith("/viewbanner"):
            await cmd_view_banner(client, message)
            return
        if txt.startswith("/viewbanied"):
            await cmd_viewbanied(client, message)
            return
        if txt.startswith("/donebanied"):
            await cmd_donebanied(client, message)
            return
        if txt.startswith("/deletebanied"):
            await cmd_deletebanied(client, message)
            return
        if txt.startswith("/setpassword"):
            await cmd_setpassword(client, message)
            return
        if txt.startswith("/reset_password"):
            await cmd_reset_password(client, message)
            return
        if txt.startswith("/setextra_pages"):
            await cmd_setextra_pages(client, message)
            return
        if txt.startswith("/pdf_edit"):
            await cmd_pdf_edit(client, message)
            return
        if txt.startswith("/addfsub"):
            await addfsub_handler(client, message)
            return
        if txt.startswith("/delfsub"):
            await delfsub_handler(client, message)
            return
        if txt.startswith("/channels"):
            await channels_handler(client, message)
            return
        if txt.startswith("/status"):
            await status_handler(client, message)
            return

    # NEW: Handle awaiting_new_password here as well (fallback)
    if session.get("awaiting_new_password"):
        pw = message.text.strip()
        session["awaiting_new_password"] = False
        if pw.lower() in {"none", "off", "disable"}:
            update_user_pdf_settings(user_id, lock_password=None)
            await client.send_message(message.chat.id, "🔓 Default password removed.")
        else:
            update_user_pdf_settings(user_id, lock_password=pw)
            await client.send_message(message.chat.id, "✅ Default password updated.")
        return

    # Fallback: extraction d'une page en image (au cas où un autre handler ne capture pas)
    if session.get("awaiting_extract_page"):
        try:
            page_no = int(message.text.strip())
        except Exception:
            await client.send_message(message.chat.id, "❌ Send a valid page number (e.g., 1)")
            return

        session["awaiting_extract_page"] = False
        file_id = session.get("file_id")
        file_name = session.get("file_name") or "document.pdf"
        user_dir = get_user_temp_dir(user_id)
        in_path = await client.download_media(file_id, file_name=user_dir / "extract_input.pdf")
        # Vérifier si le PDF est verrouillé
        if is_pdf_locked(in_path):
            await client.send_message(message.chat.id, "❌ The PDF is locked. Please unlock it first, then try again.")
            return
        out_img = str(user_dir / f"{Path(file_name).stem}_page{page_no}.png")
        try:
            extract_page_to_png(in_path, page_no, out_img, zoom=2.0)
            await client.send_photo(message.chat.id, out_img, caption=f"🖼️ Page {page_no}")
        except Exception as e:
            await client.send_message(message.chat.id, f"❌ Cannot extract page {page_no}: {e}")
        return

    # ✅ FIX: Mot de passe pour UNLOCK
    if session.get('action') == "unlock":
        password = message.text.strip()
        # Appel à la fonction de traitement unlock
        await process_unlock(client, message, session, password)
        # Remet à zéro le flag
        session.pop('action', None)
        return

    # Batch UNLOCK - receive password
    if session.get('awaiting_batch_password'):
        password = message.text.strip()
        session.pop('awaiting_batch_password', None)
        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass
        await process_batch_unlock(client, message, user_id, password)
        return

    # Batch LOCK - receive password
    if session.get('awaiting_batch_lock_password'):
        lock_pw = message.text.strip()
        session.pop('awaiting_batch_lock_password', None)
        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass
        await process_batch_lock(client, message, user_id, lock_pw)
        return

    # Gestion du renommage de vidéo
    if session.get('awaiting_video_name'):
        new_name = message.text.strip()
        
        # Assurer que le nom a une extension vidéo
        if not any(new_name.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm']):
            # Ajouter .mp4 par défaut si pas d'extension
            base_name = session.get('video_file_name', 'video.mp4')
            ext = os.path.splitext(base_name)[1] or '.mp4'
            new_name = f"{new_name}{ext}"
        
        video_file_id = session.get('video_file_id')
        
        if not video_file_id:
            await client.send_message(message.chat.id, "❌ Video session expired.")
            sessions[user_id].pop('awaiting_video_name', None)
            return
        
        try:
            # Supprimer le message de l'utilisateur
            try:
                await message.delete()
            except:
                pass
            
            # Supprimer le message d'instruction
            if 'video_edit_message_id' in session:
                try:
                    await client.delete_messages(message.chat.id, session['video_edit_message_id'])
                except:
                    pass
            
            # Envoyer le message de succès
            await client.send_message(
                message.chat.id,
                f"✅ Video renamed successfully to: {new_name}"
            )
            
            # Marquer qu'on traite un fichier
            await set_just_processed_flag(user_id)
            
            # Envoyer la vidéo avec le nouveau nom comme caption + username
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            final_caption = clean_caption_with_username(new_name, user_id)
            sent = await client.send_video(
                chat_id=message.chat.id,
                video=video_file_id,
                caption=final_caption
            )
            
            logger.info(f"✅ Video sent with new name: {final_caption}")
            
            # Planifier la suppression si nécessaire
            if delay > 0:
                async def delete_after_delay():
                    await asyncio.sleep(delay)
                    try:
                        await sent.delete()
                    except Exception as e:
                        logger.error(f"Error deleting video message: {e}")
                
                asyncio.create_task(delete_after_delay())
            
            # Le flag est géré par set_just_processed_flag
        except Exception as e:
            logger.error(f"Error renaming video: {e}")
            await client.send_message(message.chat.id, f"❌ Error: {str(e)}")
        finally:
            # Nettoyer la session
            for key in ['awaiting_video_name', 'video_file_id', 'video_file_name', 'video_message_id', 'video_edit_message_id']:
                sessions[user_id].pop(key, None)
            # IMPORTANT: Supprimer complètement la session
            sessions.pop(user_id, None)
        
        return

    # --- NEW: Manual page entry for 'both' action ---
    if session.get("awaiting_both_manual_page"):
        page_str = message.text.strip()
        try:
            page_num = int(page_str)  # Keep 1-based for unified logic
            file_id = session.get('file_id')
            if not file_id:
                await message.reply_text("❌ No PDF in session.")
            else:
                # Use unified removal logic; it will handle locks and bounds
                await remove_page_logic(client, message, user_id, page_num)
        except ValueError:
            await message.reply_text("⚠️ Invalid page number.")
        session["awaiting_both_manual_page"] = False
        return

    # Batch 'The Both' - Step 1: receive password
    if session.get('awaiting_batch_both_password'):
        password = message.text.strip()
        session['batch_both_password'] = password
        session.pop('awaiting_batch_both_password', None)
        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass
        # Ask for page selection with unified buttons
        try:
            await client.send_message(
                message.chat.id,
                "Step 2/2: Select pages to remove for all PDFs (or choose manual).",
                reply_markup=get_batch_both_pages_buttons(user_id)
            )
        except Exception as e:
            logger.error(f"Error showing batch both pages buttons: {e}")
            await client.send_message(message.chat.id, "Send pages to remove like 1,3-5 or 'none'.")
            session['awaiting_batch_both_pages'] = True
        return

    # Batch 'The Both' - Step 2: manual pages entry
    if session.get('awaiting_batch_both_pages'):
        pages_text = message.text.strip()
        session['awaiting_batch_both_pages'] = False
        password = session.get('batch_both_password', '')
        if not password:
            await client.send_message(message.chat.id, "❌ Missing password. Please restart with /process → Batch → The Both.")
            return
        await process_batch_both(client, message, user_id, password, pages_text)
        return

    # Gestion username/hashtag (paramètre)
    if session.get('awaiting_username'):
        username = message.text.strip()
        
        # Accepter n'importe quel texte (hashtag, emoji, username, etc.)
        if username:
            session['username'] = username
            session['awaiting_username'] = False
            # Reset structured state as well
            session['state'] = UserState.IDLE.value
            session.pop('state_data', None)
            
            # NEW: Save persistently
            if save_username(user_id, username):
                await client.send_message(message.chat.id, f"✅ Tag saved: {username}")
            else:
                await client.send_message(message.chat.id, f"✅ Tag saved in session: {username}\n⚠️ (Could not save to file)")
            
            logger.info(f"🔧 Tag registered for user {user_id}: {username}")
        else:
            await client.send_message(message.chat.id, "❌ Please send some text to use as your tag.")
        return

    # Gestion de la saisie manuelle des pages pour l'action "pages"
    if session.get('awaiting_pages_manual'):
        pages_text = message.text.strip()
        session['awaiting_pages_manual'] = False
        await process_pages(client, message, session, pages_text)
        return

    # 🛠️ THE BOTH - Gestion séquentielle corrigée
    # Étape 1: Réception du mot de passe
    if session.get('awaiting_both_password'):
        password = message.text.strip()
        session['both_password'] = password
        # Mirror into full_password so full_* buttons work in this flow
        session['full_password'] = password
        session.pop('awaiting_both_password', None)
        session['awaiting_both_pages'] = True
        
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
        # Show quick selection buttons under the prompt
        try:
            await client.send_message(
                message.chat.id,
                "✅ Password noted. Now send the pages to remove (e.g. `1,3-5`):",
                reply_markup=get_full_pages_buttons(user_id)
            )
        except Exception as e:
            logger.warning(f"Couldn't attach full pages buttons: {e}")
            await client.send_message(
                message.chat.id,
                "✅ Password noted. Now send the pages to remove (e.g. `1,3-5`):"
            )
        return

    # Étape 2: Réception des pages et traitement final
    elif session.get('awaiting_both_pages'):
        pages_text = message.text.strip()
        pages_to_remove, error = parse_pages_text(pages_text)
        if error:
            await client.send_message(message.chat.id, f"❌ {error}")
            return

        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass

        # Validate file presence
        if not session.get('file_id'):
            await client.send_message(message.chat.id, "❌ No file in session. Send a PDF first.")
            # Clean flags
            session.pop('awaiting_both_pages', None)
            session.pop('both_password', None)
            return

        # Gather unlock password and run unified full pipeline
        password = session.get('both_password', '')
        # Clear both-specific flags before launching pipeline
        session.pop('awaiting_both_pages', None)
        session.pop('both_password', None)
        await run_full_pipeline_and_send(client, message.chat.id, user_id, unlock_pw=password, pages_spec=pages_text, lock_pw=None)
        return

    # ⚡ FULL PROCESS - Step 1: receive unlock password
    if session.get('awaiting_full_password'):
        logger.info(f"[fullproc] Received unlock password for user %s", user_id)
        unlock_pw = message.text.strip()
        session['full_password'] = unlock_pw
        session.pop('awaiting_full_password', None)
        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass
        # Ask for page selection
        try:
            await client.send_message(
                message.chat.id,
                "Step 2/2: Select pages to remove (or choose manual).",
                reply_markup=get_full_pages_buttons(user_id)
            )
            logger.info(f"[fullproc] Page selection UI shown for user %s", user_id)
        except Exception as e:
            logger.error(f"Error showing full pages buttons: {e}")
            await client.send_message(message.chat.id, "Send pages to remove like 1,3-5 or 'none'.")
            session['awaiting_full_manual_pages'] = True
        return

    # ⚡ FULL PROCESS - Manual pages entry
    if session.get('awaiting_full_manual_pages'):
        logger.info(f"[fullproc] Manual pages received for user %s", user_id)
        pages_spec = message.text.strip()
        session['awaiting_full_manual_pages'] = False
        # Delete user's message
        try:
            await message.delete()
        except:
            pass
        pw = session.get('full_password', '')
        # If batch fullproc, handle lock pw once then process all
        if session.get('fullproc_is_batch'):
            default_pw = get_user_pdf_settings(user_id).get("lock_password")
            if not default_pw:
                # Auto-continue without locking when no default password is set
                try:
                    await client.send_message(message.chat.id, "ℹ️ No default password set — proceeding without lock.")
                except Exception:
                    pass
                await run_full_pipeline_batch_and_send(client, message.chat.id, user_id, pw, pages_spec=pages_spec, lock_pw=None)
                return
            await run_full_pipeline_batch_and_send(client, message.chat.id, user_id, pw, pages_spec=pages_spec, lock_pw=default_pw)
            return
        await run_full_pipeline_and_send(client, message.chat.id, user_id, pw, pages_spec=pages_spec)
        return

    # ⚡ FULL PROCESS - Awaiting lock password (triggered if no default)
    if session.get('awaiting_full_lock_password'):
        logger.info(f"[fullproc] Lock password received for user %s", user_id)
        session['awaiting_full_lock_password'] = False
        lock_pw = message.text.strip()
        # Delete user's message for privacy
        try:
            await message.delete()
        except:
            pass
        pending = session.pop('full_pipeline_pending', None) or {}
        pages_spec = pending.get('pages_spec', 'none')
        chat_id = pending.get('chat_id', message.chat.id)
        pw = session.get('full_password', '')
        # Normalize skip/none values
        if str(lock_pw).strip().lower() in {"skip", "none", "no"}:
            lock_pw = None
        # If pending batch flag, process all; otherwise just the current file
        if pending.get('batch'):
            await run_full_pipeline_batch_and_send(client, chat_id, user_id, pw, pages_spec=pages_spec, lock_pw=lock_pw)
        else:
            await run_full_pipeline_and_send(client, chat_id, user_id, pw, pages_spec=pages_spec, lock_pw=lock_pw)
        return

    # Gestion du mot de passe pour suppression de pages
    if session.get('awaiting_password_for_pages'):
        password = message.text.strip()
        pages_to_remove = session.get('pages_to_remove', set())
        await process_pages_with_password(client, message, session, password, pages_to_remove)
        return

    # Gestion des actions PDF classiques
    if user_id not in sessions:
        return
    action = session.get('action')
    if not action:
        return
    if action == "unlock":
        await process_unlock(client, message, session, message.text)
    elif action == "pages":
        await process_pages(client, message, session, message.text)
    elif action == "both":
        # Cette action est maintenant gérée par les états awaiting_both_password/awaiting_both_pages
        await client.send_message(message.chat.id, "❌ Invalid flow for 'both' action. Please use the menu options.")
        sessions.pop(user_id, None)
        return

# Fonctions de traitement batch
async def process_batch_unlock(client, message, user_id, password):
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        clear_processing_flag(user_id, source="batch_unlock", reason="no_pdfs")
        return
    
    # Récupérer le username depuis la session
    session = ensure_session_dict(user_id)
    username = session.get('username', '')
    
    logger.info(f"🔍 Start process_batch_unlock - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"unlocked_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with pikepdf.open(input_path, password=password if password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
                        pdf.save(output_path)
                    # Multi-banner cleaning (async offload with timeout)
                    try:
                        raw_bytes = Path(output_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                            cleaned_path = Path(temp_dir) / f"unlocked_cleaned_{file_info['file_name']}"
                            with open(cleaned_path, "wb") as cf:
                                cf.write(cleaned)
                            output_path = cleaned_path
                    except asyncio.TimeoutError as te:
                        logger.warning(f"[batch_unlock] Cleaning timed out for user {user_id}: {te}")
                    except Exception as e:
                        logger.warning(f"[batch_unlock] Cleaning failed/skipped for user {user_id}: {e}")
                    
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch unlock file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('awaiting_batch_password', None)
        clear_processing_flag(user_id, source="batch_unlock", reason="completed")

async def process_batch_pages(client, message, user_id, pages_text):
    # ... (rest of the code remains the same)

    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"modified_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with pikepdf.open(input_path, allow_overwriting_input=True) as pdf:
                        pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]

                        if not pages_to_keep:
                            error_count += 1
                            continue
                        
                        new_pdf = pikepdf.new()
                        for page in pages_to_keep:
                            new_pdf.pages.append(page)
                            
                        new_pdf.save(output_path)
                        # Multi-banner cleaning (async offload with timeout)
                        try:
                            raw_bytes = Path(output_path).read_bytes()
                            cleaned = await run_in_thread_with_timeout(
                                clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                            )
                            if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                                cleaned_path = Path(temp_dir) / f"modified_cleaned_{file_info['file_name']}"
                                with open(cleaned_path, "wb") as cf:
                                    cf.write(cleaned)
                                output_path = cleaned_path
                        except asyncio.TimeoutError as te:
                            logger.warning(f"[batch_pages] Cleaning timed out for user {user_id}: {te}")
                        except Exception as e:
                            logger.warning(f"[batch_pages] Cleaning failed/skipped for user {user_id}: {e}")
                    
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch pages file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        clear_processing_flag(user_id, source="batch_pages", reason="completed")

async def process_batch_both(client, message, user_id, password, pages_text):
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        clear_processing_flag(user_id, source="batch_both", reason="no_pdfs")
        return
    
    # Parser les pages
    pages_to_remove = set()
    try:
        for part in pages_text.replace(' ', '').split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                pages_to_remove.update(range(start, end + 1))
            else:
                pages_to_remove.add(int(part))
    except ValueError:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ Invalid format. Use: 1,3,5 or 1-5")
        else:
            await client.send_message(message.chat.id, "❌ Invalid format. Use: 1,3,5 or 1-5")
        clear_processing_flag(user_id, source="batch_both", reason="invalid_pages_format")
        return
    
    # Récupérer le username depuis la session
    session = ensure_session_dict(user_id)
    username = session.get('username', '')
    
    logger.info(f"🔍 Start process_batch_both - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Combined processing of {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"both_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with pikepdf.open(input_path, password=password if password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
                        total_pages = len(pdf.pages)
                        
                        pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]
                        
                        if not pages_to_keep:
                            error_count += 1
                            continue
                        
                        # Create a new PDF with the remaining pages
                        new_pdf = pikepdf.new()
                        for page in pages_to_keep:
                            new_pdf.pages.append(page)
                        
                        new_pdf.save(output_path)
                        # Multi-banner cleaning (async offload with timeout)
                        try:
                            raw_bytes = Path(output_path).read_bytes()
                            cleaned = await run_in_thread_with_timeout(
                                clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                            )
                            if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                                cleaned_path = Path(temp_dir) / f"both_cleaned_{file_info['file_name']}"
                                with open(cleaned_path, "wb") as cf:
                                    cf.write(cleaned)
                                output_path = cleaned_path
                        except asyncio.TimeoutError as te:
                            logger.warning(f"[batch_both] Cleaning timed out for user {user_id}: {te}")
                        except Exception as e:
                            logger.warning(f"[batch_both] Cleaning failed/skipped for user {user_id}: {e}")
                        
                        new_file_name = build_final_filename(user_id, file_info['file_name'])
                        delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                        await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                        
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch both file {i}: {e}")
                error_count += 1

        # Summary after processing all files
        try:
            await status.edit_text(
                f"✅ Processing complete!\n\n"
                f"Successful: {success_count}\n"
                f"Errors: {error_count}"
            )
        except Exception:
            pass
    finally:
        # Cleanup batch and reset flags
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        clear_processing_flag(user_id, source="batch_both", reason="completed")

async def process_batch_clean(client, message, user_id):
    """Clean @username and hashtags in all PDFs in the batch and send them back."""
    if user_id not in user_batches:
        user_batches[user_id] = []

    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]

    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        clear_processing_flag(user_id, source="batch_clean", reason="no_pdfs")
        return

    session = ensure_session_dict(user_id)
    logger.info(f"🔍 Start process_batch_clean - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Cleaning {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0

    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")

                # Download
                file = await client.download_media(
                    file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf"
                )

                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"cleaned_{file_info['file_name']}"
                    shutil.move(file, input_path)

                    # Best-effort cleaning
                    try:
                        raw_bytes = Path(input_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                            with open(output_path, "wb") as cf:
                                cf.write(cleaned)
                        else:
                            # No change – keep original
                            output_path = input_path
                    except asyncio.TimeoutError as te:
                        logger.warning(f"[batch_clean] Cleaning timed out for user {user_id}: {te}")
                        output_path = input_path
                    except Exception as e:
                        logger.warning(f"[batch_clean] Cleaning failed/skipped for user {user_id}: {e}")
                        output_path = input_path

                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)

                    success_count += 1
            except Exception as e:
                logger.error(f"Error batch clean file {i}: {e}")
                error_count += 1

        try:
            await status.edit_text(
                f"✅ Processing complete!\n\n"
                f"Successful: {success_count}\n"
                f"Errors: {error_count}"
            )
        except Exception:
            pass
    finally:
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        clear_processing_flag(user_id, source="batch_clean", reason="completed")

async def process_batch_add_banner(client, message, user_id):
    """Add configured banner to all PDFs in the batch, with pre-cleaning and final send."""
    if user_id not in user_batches:
        user_batches[user_id] = []
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]

    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        clear_processing_flag(user_id, source="batch_add_banner", reason="no_pdfs")
        return

    banner_pdf = _ensure_banner_pdf_path(user_id)
    if not banner_pdf:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No default banner. Use /setbanner first.")
        else:
            await client.send_message(message.chat.id, "❌ No default banner. Use /setbanner first.")
        clear_processing_flag(user_id, source="batch_add_banner", reason="no_default_banner")
        return

    session = ensure_session_dict(user_id)
    logger.info(f"🔍 Start process_batch_add_banner - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0

    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")

                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")

                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"bannered_{file_info['file_name']}"
                    shutil.move(file, input_path)

                    # Pre-clean banners (best-effort)
                    cleaned_input = input_path
                    try:
                        raw_bytes = Path(input_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned:
                            cleaned_path = Path(temp_dir) / "input_cleaned.pdf"
                            with open(cleaned_path, "wb") as cf:
                                cf.write(cleaned)
                            cleaned_input = cleaned_path
                    except asyncio.TimeoutError as te:
                        logger.warning(f"[batch_add_banner] Cleaning timed out for user {user_id}: {te}")
                    except Exception as e:
                        logger.warning(f"[batch_add_banner] Cleaning failed/skipped for user {user_id}: {e}")

                    # Add banner pages
                    await run_in_thread_with_timeout(
                        add_banner_pages_to_pdf, str(cleaned_input), str(output_path), banner_pdf, place='after', timeout=BANNER_ADD_TIMEOUT
                    )

                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)

                    success_count += 1
            except Exception as e:
                logger.error(f"Error batch add banner file {i}: {e}")
                error_count += 1

        try:
            await status.edit_text(
                f"✅ Processing complete!\n\n"
                f"Successful: {success_count}\n"
                f"Errors: {error_count}"
            )
        except Exception:
            pass
    finally:
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('awaiting_batch_lock_password', None)
        clear_processing_flag(user_id, source="batch_add_banner", reason="completed")

async def process_batch_lock(client, message, user_id, password: str):
    """Lock all PDFs in the batch with the provided password, then send."""
    if user_id not in user_batches:
        user_batches[user_id] = []

    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]

    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        clear_processing_flag(user_id, source="batch_lock", reason="no_pdfs")
        return

    # Support 'default' keyword to use saved password if present
    if password.strip().lower() == 'default':
        password = (get_user_pdf_settings(user_id) or {}).get('lock_password')

    skip_lock = False
    if not password:
        skip_lock = True
        # Inform and continue without locking
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("ℹ️ No password provided — proceeding without lock.")
        else:
            await client.send_message(message.chat.id, "ℹ️ No password provided — proceeding without lock.")

    session = ensure_session_dict(user_id)
    logger.info(f"🔍 Start process_batch_lock - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Locking {len(pdf_files)} PDF files...")
    success_count = 0
    error_count = 0

    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(pdf_files)}...")

                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")

                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"locked_{file_info['file_name']}"
                    shutil.move(file, input_path)

                    # Lock the PDF (or skip locking if no password)
                    try:
                        if not skip_lock:
                            lock_pdf_with_password(str(input_path), str(output_path), password)
                        else:
                            shutil.copy(str(input_path), str(output_path))
                    except Exception as e:
                        logger.error(f"[batch_lock] Error locking file {i}: {e}")
                        error_count += 1
                        continue

                    # Optional post-clean before sending (best-effort)
                    try:
                        raw_bytes = Path(output_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                            cleaned_path = Path(temp_dir) / f"locked_cleaned_{file_info['file_name']}"
                            with open(cleaned_path, "wb") as cf:
                                cf.write(cleaned)
                            output_path = cleaned_path
                    except asyncio.TimeoutError as te:
                        logger.warning(f"[batch_lock] Cleaning timed out for user {user_id}: {te}")
                    except Exception as e:
                        logger.warning(f"[batch_lock] Cleaning failed/skipped for user {user_id}: {e}")

                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)

                    success_count += 1
            except Exception as e:
                logger.error(f"Error batch lock file {i}: {e}")
                error_count += 1

        try:
            await status.edit_text(
                f"✅ Processing complete!\n\n"
                f"Successful: {success_count}\n"
                f"Errors: {error_count}"
            )
        except Exception:
            pass
    finally:
        clear_user_batch(user_id)
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('awaiting_batch_lock_password', None)
        clear_processing_flag(user_id, source="batch_lock", reason="completed")

async def process_pages(client, message, session, pages_text):
    """Remove selected pages from a PDF without password.
    If the PDF is protected, instruct the user to unlock first or provide a password.
    """
    user_id = message.from_user.id
    status = None
    try:
        # Delete user's message for privacy
        try:
            await message.delete()
        except Exception:
            pass

        logger.info(f"🗑️ process_pages - User {user_id}")

        status = await client.send_message(message.chat.id, MESSAGES['processing'])

        # Ensure we have a file in session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return

        # Parse pages
        pages_to_remove, error = parse_pages_text(pages_text)
        if error:
            await status.edit_text(f"❌ {error}")
            return

        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"pages_{session['file_name']}"
            shutil.move(file, input_path)

            try:
                with pikepdf.open(input_path, allow_overwriting_input=True) as pdf:
                    username = session.get('username', '')

                    pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]
                    if not pages_to_keep:
                        await status.edit_text("❌ No pages remaining after removal.")
                        return

                    new_pdf = pikepdf.new()
                    for page in pages_to_keep:
                        new_pdf.pages.append(page)
                    new_pdf.save(output_path)

                    # Multi-banner cleaning (async offload with timeout)
                    try:
                        raw_bytes = Path(output_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                            cleaned_path = Path(temp_dir) / f"pages_cleaned_{session['file_name']}"
                            with open(cleaned_path, "wb") as cf:
                                cf.write(cleaned)
                            output_path = cleaned_path
                    except asyncio.TimeoutError as e:
                        logger.warning(f"[process_pages] Banner cleaning timed out for user {user_id}: {e}")
                    except Exception as e:
                        logger.exception(f"[process_pages] Banner cleaning failed for user {user_id}: {e}")
            except pikepdf.PasswordError:
                await status.edit_text("❌ PDF is protected. Please use 'Unlock' first or provide a password.")
                return

            # Build final filename
            cleaned_name = build_final_filename(user_id, session['file_name'])

            # Remove status message
            try:
                await status.delete()
            except Exception:
                pass

            # Inform success
            await client.send_message(
                message.chat.id,
                f"✅ Pages {pages_text} removed successfully!"
            )

            # Send file
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)

    except Exception as e:
        logger.error(f"Error process_pages: {e}")
        try:
            if status:
                await status.edit_text(MESSAGES['error'])
        except Exception:
            pass
    finally:
        # IMPORTANT: Clear session to mirror password flow behavior
        sessions.pop(user_id, None)

async def process_pages_with_password(client, message, session, password, pages_to_remove):
    """Traite la suppression de pages avec mot de passe"""
    user_id = message.from_user.id
    
    try:
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
        logger.info(f"🗑️ process_pages_with_password - User {user_id}")
        
        status = await client.send_message(message.chat.id, MESSAGES['processing'])
        
        # Vérifier l'existence du fichier dans la session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return
        
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"pages_{session['file_name']}"
            shutil.move(file, input_path)
            
            try:
                with pikepdf.open(input_path, password=password if password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
                    total_pages = len(pdf.pages)
                    username = session.get('username', '')
                    
                    pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]
                    
                    if not pages_to_keep:
                        await status.edit_text("❌ No pages remaining after removal.")
                        return
                    
                    new_pdf = pikepdf.new()
                    for page in pages_to_keep:
                        new_pdf.pages.append(page)
                    
                    new_pdf.save(output_path)
                    # Multi-banner cleaning (async offload with timeout)
                    try:
                        raw_bytes = Path(output_path).read_bytes()
                        cleaned = await run_in_thread_with_timeout(
                            clean_pdf_banners, raw_bytes, user_id, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                        )
                        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
                            cleaned_path = Path(temp_dir) / f"pages_cleaned_{session['file_name']}"
                            with open(cleaned_path, "wb") as cf:
                                cf.write(cleaned)
                            output_path = cleaned_path
                    except asyncio.TimeoutError as e:
                        logger.warning(f"[process_pages_with_password] Banner cleaning timed out for user {user_id}: {e}")
                    except Exception as e:
                        logger.exception(f"[process_pages_with_password] Banner cleaning failed for user {user_id}: {e}")
            except pikepdf.PasswordError:
                await status.edit_text("❌ Incorrect password.")
                return
            
            # Nettoyer le nom du fichier
            cleaned_name = build_final_filename(user_id, session['file_name'])
            
            # Supprimer le message de statut
            try:
                await status.delete()
            except:
                pass
            
            # Envoyer le message de succès
            await client.send_message(
                message.chat.id,
                f"✅ Pages removed successfully with password!"
            )
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                    
    except Exception as e:
        logger.error(f"Error process_pages_with_password: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

async def process_both(client, message, session, text):
    """Traite l'action 'both' - déverrouillage + suppression de pages + nettoyage"""
    user_id = message.from_user.id
    
    try:
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
        logger.info(f"🛠️ process_both - User {user_id}")
        
        status = await client.send_message(message.chat.id, MESSAGES['processing'])
        
        # Vérifier l'existence du fichier dans la session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return
        
        # Pour l'action 'both', on attend d'abord les pages, puis le mot de passe
        # Cette fonction ne devrait pas être appelée directement
        await status.edit_text("❌ Invalid flow for 'both' action. Please use the menu options.")
                    
    except Exception as e:
        logger.error(f"Error process_both: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

# ===== Commands: setbanner / view_banner / setpassword / reset_password / setextra_pages =====
@app.on_message(filters.command(["setbanner"]) & filters.private)
@admin_only
async def cmd_setbanner(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    sessions.setdefault(uid, {})["awaiting_banner_upload"] = True
    await client.send_message(message.chat.id, "🖼️ Envoie-moi ta bannière (image ou PDF d’une page).")


@app.on_message(filters.command(["view_banner", "viewbanner"]) & filters.private)
@admin_only
async def cmd_view_banner(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    bp = get_user_pdf_settings(uid).get("banner_path")
    if not bp or not os.path.exists(bp):
        await client.send_message(message.chat.id, "ℹ️ Aucune bannière définie. Utilise /setbanner")
        return
    if bp.lower().endswith(".pdf"):
        await client.send_document(message.chat.id, bp, caption="📄 Bannière (PDF)")
    else:
        await client.send_photo(message.chat.id, bp, caption="🖼️ Bannière (image)")


@app.on_message(filters.command(["setpassword"]) & filters.private)
@admin_only
async def cmd_setpassword(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        # Interactive mode: ask for password now
        sessions.setdefault(uid, {})["awaiting_new_password"] = True
        await client.send_message(
            message.chat.id,
            "🔐 Send your default password now (or type 'none' to disable)."
        )
        return
    arg = parts[1].strip()
    if arg.lower() in ("none", "off", "disable"):
        update_user_pdf_settings(uid, lock_password=None)
        await client.send_message(message.chat.id, "🔓 Default password removed.")
    else:
        update_user_pdf_settings(uid, lock_password=arg)
        await client.send_message(message.chat.id, "🔐 Default password saved.")


@app.on_message(filters.command(["reset_password"]) & filters.private)
@admin_only
async def cmd_reset_password(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) >= 2:
        update_user_pdf_settings(uid, lock_password=parts[1].strip())
        await client.send_message(message.chat.id, "🔐 New default password saved.")
        return
    sessions.setdefault(uid, {})["awaiting_new_password"] = True
    await client.send_message(message.chat.id, "🔐 Send the new default password now.")


# ===== Multi-banner management commands =====
@app.on_message(filters.command(["setbanied", "setbaneid"]) & filters.private)
async def cmd_setbanied(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    ensure_banied_dir(uid)
    BANIED_ADD_MODE.add(uid)
    await client.send_message(message.chat.id, "📨 Envoie moi le banners")


@app.on_message(filters.command(["donebanied"]) & filters.private)
async def cmd_donebanied(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    BANIED_ADD_MODE.discard(uid)
    cnt = len(list_banied_images(uid))
    await client.send_message(message.chat.id, f"✅ Mode d’ajout de bannières désactivé. Tu as {cnt} bannière(s).")


@app.on_message(filters.command(["viewbanied"]) & filters.private)
async def cmd_viewbanied(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    imgs = list_banied_images(uid)
    if not imgs:
        await client.send_message(message.chat.id, "ℹ️ Aucune bannière enregistrée. Utilise /setbanied pour en ajouter.")
        return
    # Send a short summary and first few images
    await client.send_message(message.chat.id, f"🧾 Tu as {len(imgs)} bannière(s). Affichage des 5 premières…")
    for i, p in enumerate(imgs[:5], start=1):
        try:
            await client.send_photo(message.chat.id, str(p), caption=f"#{i}: {p.name}")
        except Exception:
            try:
                await client.send_document(message.chat.id, str(p), caption=f"#{i}: {p.name}")
            except Exception:
                pass


@app.on_message(filters.command(["deletebanied"]) & filters.private)
async def cmd_deletebanied(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        # No argument: smart behavior
        imgs = list_banied_images(uid)
        if not imgs:
            await client.send_message(message.chat.id, "ℹ️ Aucune bannière enregistrée. Utilise /setbanied pour en ajouter.")
            return
        if len(imgs) == 1:
            # Auto-delete the only one
            n = delete_banied(uid, 1)
            if n:
                await client.send_message(message.chat.id, "🗑️ Bannière #1 supprimée.")
            else:
                await client.send_message(message.chat.id, "❌ Échec de suppression de la bannière #1.")
            return
        # Multiple images: show selection keyboard
        buttons = []
        max_buttons = min(len(imgs), 25)
        for i in range(1, max_buttons + 1):
            buttons.append(InlineKeyboardButton(str(i), callback_data=f"banied_del:{i}"))
        # Group by 5 per row
        rows = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
        rows.append([
            InlineKeyboardButton("All", callback_data="banied_del:all"),
            InlineKeyboardButton("Cancel", callback_data="banied_del:cancel"),
        ])
        await client.send_message(
            message.chat.id,
            "🧹 Sélectionne la bannière à supprimer:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return
    arg = args[1].strip().lower()
    if arg in {"all", "*"}:
        n = delete_banied(uid, None)
        await client.send_message(message.chat.id, f"🗑️ {n} bannière(s) supprimée(s).")
        return
    try:
        idx = int(arg)
    except Exception:
        await client.send_message(message.chat.id, "❌ Index invalide. Utilise un nombre ou 'all'.")
        return
    n = delete_banied(uid, idx)
    if n:
        await client.send_message(message.chat.id, f"🗑️ Bannière #{idx} supprimée.")
    else:
        await client.send_message(message.chat.id, f"❌ Bannière #{idx} introuvable.")

@app.on_callback_query(filters.regex(r"^banied_del:(.+)$"))
async def cb_banied_del(client, query: CallbackQuery):
    uid = query.from_user.id
    # Force-join check for callbacks
    if not await is_user_in_channel(uid):
        try:
            await query.answer("Please join the channel to use this.", show_alert=True)
        except Exception:
            pass
        try:
            await send_force_join_message(client, query.message)
        except Exception:
            pass
        return
    try:
        data = query.data.split(":", 1)[1]
    except Exception:
        await query.answer("Invalid action", show_alert=True)
        return
    if data == "cancel":
        try:
            await query.edit_message_text("❎ Suppression annulée.")
        except Exception:
            pass
        return
    if data in {"all", "*"}:
        n = delete_banied(uid, None)
        try:
            await query.edit_message_text(f"🗑️ {n} bannière(s) supprimée(s).")
        except Exception:
            pass
        return
    try:
        idx = int(data)
    except Exception:
        await query.answer("Index invalide.", show_alert=True)
        return
    n = delete_banied(uid, idx)
    if n:
        try:
            await query.edit_message_text(f"🗑️ Bannière #{idx} supprimée.")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(f"❌ Bannière #{idx} introuvable.")
        except Exception:
            pass


@app.on_message(filters.command(["setextra_pages"]) & filters.private)
@admin_only
async def cmd_setextra_pages(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    session = sessions.get(uid, {})
    if not session.get("file_id"):
        await client.send_message(message.chat.id, "📄 Send a PDF first.")
        return
    sessions.setdefault(uid, {})["awaiting_extract_page"] = True
    await client.send_message(message.chat.id, "📄 Which page number should I extract as image? (e.g., 1)")


# Extend existing text handler with awaited states (password + page number + extract + new password)
@app.on_message(filters.text & filters.private)
async def on_text_extensions(client, message: Message):
    uid = message.from_user.id
    chat_id = message.chat.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    sess = sessions.setdefault(uid, {})

    if sess.get("awaiting_new_password"):
        pw = message.text.strip()
        sess["awaiting_new_password"] = False
        if pw.lower() in {"none", "off", "disable"}:
            update_user_pdf_settings(uid, lock_password=None)
            await client.send_message(chat_id, "🔓 Default password removed.")
        else:
            update_user_pdf_settings(uid, lock_password=pw)
            await client.send_message(chat_id, "✅ Default password updated.")
        return

    if sess.get("awaiting_extract_page"):
        try:
            page_no = int(message.text.strip())
        except Exception:
            await client.send_message(chat_id, "❌ Send a valid page number (e.g., 1)")
            return
        sess["awaiting_extract_page"] = False
        # download current pdf and send the page as image
        file_id = sess.get("file_id")
        file_name = sess.get("file_name") or "document.pdf"
        user_dir = get_user_temp_dir(uid)
        in_path = await client.download_media(file_id, file_name=user_dir / "extract_input.pdf")
        # If PDF is locked, ask to unlock first
        if is_pdf_locked(in_path):
            await client.send_message(chat_id, "❌ The PDF is locked. Please unlock it first, then try again.")
            return
        out_img = str(user_dir / f"{Path(file_name).stem}_page{page_no}.png")
        try:
            extract_page_to_png(in_path, page_no, out_img, zoom=2.0)
            await client.send_photo(chat_id, out_img, caption=f"🖼️ Page {page_no}")
        except Exception as e:
            await client.send_message(chat_id, f"❌ Cannot extract page {page_no}: {e}")
        return


# Capture of banner upload (photo or document)
@app.on_message(filters.photo & filters.private)
async def on_photo_maybe_banner(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    # New: multi-banner add mode
    if uid in BANIED_ADD_MODE:
        try:
            user_dir = ensure_banied_dir(uid)
            # Use highest resolution photo
            photo = message.photo
            path = await client.download_media(photo.file_id, file_name=user_dir / f"banied_{int(time.time())}_{message.id}.jpg")
            await client.send_message(message.chat.id, "✅ Bannière enregistrée pour détection.")
        except Exception as e:
            await client.send_message(message.chat.id, f"❌ Échec de l'enregistrement de la bannière : {e}")
        return
    if sessions.get(uid, {}).get("awaiting_banner_upload"):
        path = await client.download_media(message.photo.file_id, file_name=BANNERS_DIR / f"banner_{uid}.jpg")
        update_user_pdf_settings(uid, banner_path=str(path))
        sessions[uid]["awaiting_banner_upload"] = False
        await client.send_message(message.chat.id, "✅ Image de bannière enregistrée.")
        return


# Hook inside existing document handler flow to accept banner file uploads
@app.on_message(filters.document & filters.private)
async def on_document_maybe_banner_or_pdf_forward(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    # New: multi-banner add mode (accept images or first page of PDFs)
    if uid in BANIED_ADD_MODE:
        doc = message.document
        name = (doc.file_name or "file")
        user_dir = ensure_banied_dir(uid)
        lower = name.lower()
        try:
            if (doc.mime_type or "").startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
                await client.download_media(doc.file_id, file_name=user_dir / f"banied_{int(time.time())}_{message.id}{Path(name).suffix}")
                await client.send_message(message.chat.id, "✅ Image de bannière enregistrée pour détection.")
                return
            elif lower.endswith(".pdf"):
                # Convert first page to PNG and store
                tmp_pdf = await client.download_media(doc.file_id, file_name=user_dir / f"_tmp_{message.id}.pdf")
                try:
                    if fitz is None:
                        await client.send_message(message.chat.id, "❌ PyMuPDF manquant ; impossible de convertir le PDF en image.")
                    else:
                        with fitz.open(tmp_pdf) as d:
                            if len(d) == 0:
                                await client.send_message(message.chat.id, "❌ PDF vide.")
                            else:
                                p = d[0]
                                pix = p.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                                out_path = user_dir / f"banied_{int(time.time())}_{message.id}.png"
                                pix.save(out_path)
                                await client.send_message(message.chat.id, "✅ Bannière enregistrée depuis la première page du PDF.")
                finally:
                    try:
                        os.remove(tmp_pdf)
                    except Exception:
                        pass
                return
        except Exception as e:
            await client.send_message(message.chat.id, f"❌ Échec de l'enregistrement de la bannière : {e}")
            return
    if sessions.get(uid, {}).get("awaiting_banner_upload"):
        doc = message.document
        name = (doc.file_name or "banner")
        if (doc.mime_type or "").startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")):
            path = await client.download_media(doc.file_id, file_name=BANNERS_DIR / f"banner_{uid}{Path(name).suffix}")
            update_user_pdf_settings(uid, banner_path=str(path))
            sessions[uid]["awaiting_banner_upload"] = False
            await client.send_message(message.chat.id, "✅ Image de bannière enregistrée.")
            return
        elif name.lower().endswith(".pdf"):
            path = await client.download_media(doc.file_id, file_name=BANNERS_DIR / f"banner_{uid}.pdf")
            update_user_pdf_settings(uid, banner_path=str(path))
            sessions[uid]["awaiting_banner_upload"] = False
            await client.send_message(message.chat.id, "✅ PDF de bannière enregistré.")
            return
    # If not awaiting banner, fall through to original document flow handled elsewhere


# ===== Inline buttons additions: Add banner + Lock =====

def build_pdf_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clean usernames", callback_data=f"clean_username:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
        [InlineKeyboardButton("⚡ Full Process", callback_data=f"fullproc:{user_id}")],
        [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}")],
        [InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
    ])


@app.on_callback_query(filters.regex(r"^add_banner:(\d+)$"))
async def cb_add_banner(client, query: CallbackQuery):
    # Neutralized: handled in the generic button_callback
    await query.answer()
    return
 
 
@app.on_callback_query(filters.regex(r"^lock_now:(\d+)$"))
async def cb_lock_now(client, query: CallbackQuery):
    # Neutralized: handled in the generic button_callback
    await query.answer()
    return


# ===== /pdf_edit macro command =====
@app.on_message(filters.command(["addfsub"]) & filters.private)
@admin_only
async def addfsub_handler(client, message: Message):
    track_user(message.from_user.id)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("Usage:\n`/addfsub @channel1 @channel2 ...`", quote=True)
        return
    raw = re.split(r"[,\s]+", args[1].strip())
    chans = [x for x in (s.lstrip("@").lstrip("#") for s in raw) if x]
    new_list = add_forced_channels(chans)
    await message.reply_text("✅ Forced-sub channels updated:\n" + "\n".join(f"• @{c}" for c in new_list), quote=True)

@app.on_message(filters.command(["delfsub"]) & filters.private)
@admin_only
async def delfsub_handler(client, message: Message):
    track_user(message.from_user.id)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        set_forced_channels([])
        await message.reply_text("✅ All forced-sub channels removed.", quote=True)
        return
    raw = re.split(r"[,\s]+", args[1].strip())
    chans = [x for x in (s.lstrip("@").lstrip("#") for s in raw) if x]
    new_list = del_forced_channels(chans)
    if new_list:
        await message.reply_text("✅ Remaining forced-sub channels:\n" + "\n".join(f"• @{c}" for c in new_list), quote=True)
    else:
        await message.reply_text("✅ No forced-sub channels configured.", quote=True)

@app.on_message(filters.command(["channels"]) & filters.private)
@admin_only
async def channels_handler(client, message: Message):
    track_user(message.from_user.id)
    chans = get_forced_channels()
    if not chans:
        await message.reply_text("ℹ️ No forced-sub channels configured.", quote=True)
        return
    await message.reply_text("📋 Forced-sub channels:\n" + "\n".join(f"• @{c}" for c in chans), quote=True)

@app.on_message(filters.command(["status"]) & filters.private)
async def status_handler(client, message: Message):
    track_user(message.from_user.id)
    t0 = time.time()
    ping_msg = await message.reply_text("[/status\n\nCalculating ping...]", quote=True)
    ping_ms = (time.time() - t0) * 1000.0

    uptime = fmt_uptime(time.time() - START_TIME)
    users = total_users()

    ram_pct = cpu_pct = None
    if psutil:
        try:
            ram_pct = psutil.virtual_memory().percent
            cpu_pct = psutil.cpu_percent(interval=0.2)
        except Exception:
            pass

    du = shutil.disk_usage(Path.cwd())
    disk_used = du.used
    disk_total = du.total
    disk_free = du.free
    disk_pct = (disk_used / disk_total) * 100 if disk_total else 0

    stats = get_stats()
    total_files = stats.get("files", 0)
    total_storage = stats.get("storage_bytes", 0)

    def bar(pct: float) -> str:
        pct = 0.0 if pct is None else max(0.0, min(100.0, pct))
        filled = int(round(pct / 10))
        slots = 10
        blocks = "■" * max(0, filled - 1)
        tip = "▤" if filled > 0 else ""
        empty = "□" * (slots - filled)
        return f"[{blocks}{tip}{empty}] {pct:.1f}%"

    ram_line = f"┖ {bar(ram_pct)}" if ram_pct is not None else "┖ N/A"
    cpu_line = f"┖ {bar(cpu_pct)}" if cpu_pct is not None else "┖ N/A"

    text = (
        "Bot Status\n\n"
        f"Uptime: {uptime}\n"
        f"Ping: {ping_ms:.0f} ms\n"
        f"Users: {users}\n\n"
        "RAM:\n"
        f"{ram_line}\n\n"
        "CPU:\n"
        f"{cpu_line}\n\n"
        "Disk:\n"
        f"{bar(disk_pct)}\n"
        f"Used: {format_bytes(disk_used)}\n"
        f"Free: {format_bytes(disk_free)}\n"
        f"Total: {format_bytes(disk_total)}\n\n"
        "Rename Statistics:\n"
        f"Total files renamed: {total_files:,}\n"
        f"Total storage used: {format_bytes(total_storage)}"
    )

    try:
        await ping_msg.edit_text(text)
    except Exception:
        await message.reply_text(text, quote=True)
@app.on_message(filters.command(["pdf_edit"]) & filters.private)
async def cmd_pdf_edit(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    sess = sessions.setdefault(uid, {})
    file_id = sess.get("file_id")
    file_name = sess.get("file_name") or "document.pdf"
    chat_id = message.chat.id
    if not file_id:
        await client.send_message(chat_id, "📄 Send a PDF first.")
        return
    user_dir = get_user_temp_dir(uid)
    in_path = await client.download_media(file_id, file_name=user_dir / "pdfedit_input.pdf")
    try:
        with pikepdf.open(in_path):
            pass
        sess["pdf_edit"] = {"work": in_path}
        sess["awaiting_pdf_edit_pages"] = True
        await client.send_message(
            chat_id,
            "🧹 Which pages do you want to remove?\n\nExamples:\n• 1 → removes page 1\n• 1,3,5 → removes pages 1, 3 and 5\n• 1-5 → removes pages 1 to 5",
            reply_markup=get_pdf_edit_pages_buttons(uid)
        )
    except PasswordError:
        sess["pdf_edit"] = {"work": in_path}
        sess["awaiting_pdf_edit_password"] = True
        await client.send_message(chat_id, "🔑 PDF is locked. Send password to unlock.")
@app.on_message(filters.text & filters.private)
async def on_text_pdf_edit_flow(client, message: Message):
    uid = message.from_user.id
    chat_id = message.chat.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    sess = sessions.setdefault(uid, {})

    if sess.get("awaiting_pdf_edit_password") and not sess.get("awaiting_new_password"):
        pw = message.text.strip()
        sess["awaiting_pdf_edit_password"] = False
        if not pw:
            await client.send_message(chat_id, "❌ Empty password. Retry /pdf_edit.")
            sess.pop("pdf_edit", None)
            return
        tmp_in = sess["pdf_edit"]["work"]
        user_dir = get_user_temp_dir(uid)
        unlocked = str(user_dir / "pdfedit_unlocked.pdf")
        try:
            unlock_pdf(tmp_in, unlocked, pw)
        except PasswordError:
            sess["awaiting_pdf_edit_password"] = True
            await client.send_message(chat_id, "❌ Wrong password. Try again.")
            return
        sess["pdf_edit"]["work"] = unlocked
        sess["awaiting_pdf_edit_pages"] = True
        await client.send_message(
            chat_id,
            "🧹 Which pages do you want to remove?\n\nExamples:\n• 1 → removes page 1\n• 1,3,5 → removes pages 1, 3 and 5\n• 1-5 → removes pages 1 to 5",
            reply_markup=get_pdf_edit_pages_buttons(uid)
        )
        return

    if sess.get("awaiting_pdf_edit_pages"):
        sess["awaiting_pdf_edit_pages"] = False
        await _pdf_edit_apply_pages_and_continue(client, uid, chat_id, message.text)
        return

    if sess.get("awaiting_pdf_edit_lock_password"):
        sess["awaiting_pdf_edit_lock_password"] = False
        lock_pw = message.text.strip()
        work = sess.get("pdf_edit", {}).get("work")
        if not work:
            await client.send_message(chat_id, "⚠️ Context lost. Run /pdf_edit again.")
            return
        if lock_pw.lower() in {"skip", "none", "no"}:
            await _pdf_edit_finalize_and_send(client, uid, chat_id, work, None)
            return
        await _pdf_edit_finalize_and_send(client, uid, chat_id, work, lock_pw)
        return


async def run_full_pipeline_and_send(client, chat_id: int, uid: int, unlock_pw: str | None, pages_spec: str, lock_pw: str | None = None):
    """Full Process pipeline: unlock -> add banner -> remove pages -> lock -> multi-clean -> send."""
    sess = ensure_session_dict(uid)
    if sess.get('processing'):
        await client.send_message(chat_id, "⚠️ Another process is running. Please wait.")
        return
    set_processing_flag(uid, chat_id=chat_id, source="full_pipeline")
    logger.info("[FullPipeline] START uid=%s pages_spec='%s' unlock_pw_provided=%s", uid, str(pages_spec), bool(unlock_pw))
    status = None
    try:
        status = await client.send_message(chat_id, MESSAGES['processing'])

        file_id = sess.get('file_id')
        file_name = sess.get('file_name') or 'document.pdf'
        if not file_id:
            await client.send_message(chat_id, "❌ No PDF in session. Send a PDF first.")
            return

        user_dir = get_user_temp_dir(uid)
        in_path = await client.download_media(file_id, file_name=user_dir / 'full_input.pdf')

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            unlocked = str(td_path / 'unlocked.pdf')
            # Unlock (or copy through if none)
            logger.info("[FullPipeline] Unlock step for uid=%s", uid)
            try:
                with pikepdf.open(in_path, password=(unlock_pw if unlock_pw and unlock_pw.lower() != 'none' else ''), allow_overwriting_input=True) as pdf:
                    pdf.save(unlocked)
            except pikepdf.PasswordError:
                await status.edit_text("❌ Incorrect password for unlocking.")
                return
            logger.info("[FullPipeline] Unlock OK uid=%s", uid)

            # Pre-clean: remove pages that match known banner images BEFORE adding our custom banner
            precleaned = str(td_path / 'precleaned.pdf')
            try:
                logger.info("[FullPipeline] Pre-clean banners uid=%s", uid)
                raw_bytes = Path(unlocked).read_bytes()
                cleaned_bytes = await run_in_thread_with_timeout(
                    clean_pdf_banners, raw_bytes, uid, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
                )
                if cleaned_bytes and isinstance(cleaned_bytes, (bytes, bytearray)):
                    with open(precleaned, 'wb') as cf:
                        cf.write(cleaned_bytes)
                else:
                    shutil.copy2(unlocked, precleaned)
            except asyncio.TimeoutError as e:
                logger.error(f"FullProcess preclean timeout: {e}")
                shutil.copy2(unlocked, precleaned)
            except Exception as e:
                logger.error(f"FullProcess preclean error: {e}")
                shutil.copy2(unlocked, precleaned)

            # Add banner if available
            banner_pdf = _ensure_banner_pdf_path(uid)
            bannered = str(td_path / 'bannered.pdf')
            try:
                logger.info("[FullPipeline] Banner add step uid=%s", uid)
                if banner_pdf:
                    await run_in_thread_with_timeout(
                        add_banner_pages_to_pdf, precleaned, bannered, banner_pdf, place='after', timeout=BANNER_ADD_TIMEOUT
                    )
                else:
                    shutil.copy2(precleaned, bannered)
            except asyncio.TimeoutError as e:
                logger.error(f"FullProcess banner timeout: {e}")
                shutil.copy2(precleaned, bannered)
            except Exception as e:
                logger.error(f"FullProcess banner error: {e}")
                shutil.copy2(precleaned, bannered)

            # Compute pages to remove
            pages_list: list[int] = []
            spec = (pages_spec or '').strip().lower()
            if spec in {'first', 'last', 'middle'}:
                try:
                    with pikepdf.open(bannered, allow_overwriting_input=True) as pdf:
                        n = len(pdf.pages)
                    if n > 0:
                        if spec == 'first':
                            pages_list = [1]
                        elif spec == 'last':
                            pages_list = [n]
                        else:  # middle
                            pages_list = [((n + 1) // 2)]
                except Exception as e:
                    logger.error(f"FullProcess count pages error: {e}")
            else:
                pages_list = parse_pages_spec(spec)
            logger.info("[FullPipeline] Pages computed uid=%s -> %s", uid, pages_list)

            # Remove pages if any
            paged = str(td_path / 'paged.pdf')
            try:
                logger.info("[FullPipeline] Remove pages step uid=%s (count=%d)", uid, len(pages_list) if pages_list else 0)
                if pages_list:
                    remove_pages_by_numbers(bannered, paged, pages_list)
                else:
                    shutil.copy2(bannered, paged)
            except Exception as e:
                await status.edit_text(f"❌ Error removing pages: {e}")
                return

            # Determine lock password
            final_lock_pw = None
            explicit = lock_pw is not None and str(lock_pw).strip() != ""
            if explicit:
                # User explicitly provided a value
                if lock_pw.lower() in {"none", "skip", "no"}:
                    final_lock_pw = None  # do not lock even if default exists
                else:
                    final_lock_pw = lock_pw
            else:
                # No explicit value provided; fall back to default if any, otherwise proceed without locking
                default_pw = get_user_pdf_settings(uid).get("lock_password")
                if default_pw:
                    final_lock_pw = default_pw
                else:
                    final_lock_pw = None
                    try:
                        await status.edit_text("ℹ️ No default lock password — proceeding without lock.")
                    except Exception:
                        pass

            # Finalize (lock + multi-clean + send)
            logger.info("[FullPipeline] Finalize step uid=%s lock=%s", uid, bool(final_lock_pw))
            await _pdf_edit_finalize_and_send(client, uid, chat_id, paged, final_lock_pw)

        try:
            if status:
                await status.delete()
        except:
            pass

        await client.send_message(
            chat_id,
            "✅ Full Process completed!\n• Unlocked\n• Banner added\n• Pages processed\n• Locked & Cleaned"
        )
    except Exception as e:
        logger.error(f"FullProcess error: {e}")
        if status:
            try:
                await status.edit_text(MESSAGES['error'])
            except:
                pass
    finally:
        # Always release processing flag
        clear_processing_flag(uid, source="full_pipeline", reason="completed")
        logger.info("[FullPipeline] END uid=%s (processing cleared)", uid)
        # If we're not awaiting a lock password step, clean temp full-process keys
        if not sess.get('awaiting_full_lock_password'):
            for k in (
                'awaiting_full_password',
                'awaiting_full_manual_pages',
                'full_password',
                'full_pipeline_pending',
            ):
                try:
                    sess.pop(k, None)
                except Exception:
                    pass

async def run_full_pipeline_batch_and_send(client, chat_id: int, uid: int, unlock_pw: str | None, pages_spec: str, lock_pw: str | None = None):
    """Run Full Process on all PDFs in the user's batch using the provided unlock/lock passwords and page spec."""
    sess = ensure_session_dict(uid)
    batch = user_batches.get(uid, [])
    if not batch:
        await client.send_message(chat_id, "❌ No files waiting in the batch")
        return
    # Filter PDFs (skip videos or unsupported items)
    pdf_items = [it for it in batch if not it.get('is_video')]
    if not pdf_items:
        await client.send_message(chat_id, "❌ No PDFs in the batch to process.")
        return
    for it in pdf_items:
        try:
            sess['file_id'] = it.get('file_id')
            sess['file_name'] = it.get('file_name') or 'document.pdf'
            await run_full_pipeline_and_send(client, chat_id, uid, unlock_pw, pages_spec, lock_pw=lock_pw)
        except Exception as e:
            logger.error(f"Batch FullProcess error on {it.get('file_name')}: {e}")
            try:
                await client.send_message(chat_id, f"❌ Error on {it.get('file_name')}: {e}")
            except Exception:
                pass
    # clear batch after processing
    clear_user_batch(uid)
    try:
        await client.send_message(chat_id, "✅ Batch Full Process completed for all PDFs.")
    except Exception:
        pass
    # Reset batch flag for fullproc so next runs behave as single-flow unless re-triggered
    try:
        sess['fullproc_is_batch'] = False
    except Exception:
        pass


async def _pdf_edit_apply_pages_and_continue(client, uid: int, chat_id: int, pages_spec: str):
    """Apply page selection for /pdf_edit (supports 'first'/'last'/'middle' or manual spec),
    then continue with banner add and lock prompt/finalization.
    """
    sess = ensure_session_dict(uid)
    work = sess.get("pdf_edit", {}).get("work")
    if not work:
        await client.send_message(chat_id, "⚠️ Context lost. Run /pdf_edit again.")
        return
    user_dir = get_user_temp_dir(uid)
    pruned = str(user_dir / "pdfedit_pruned.pdf")

    # Build pages list
    pages_list: list[int] = []
    spec = (pages_spec or "").strip().lower()
    if spec in {"first", "last", "middle"}:
        try:
            with pikepdf.open(work) as pdf:
                n = len(pdf.pages)
            if n > 0:
                if spec == "first":
                    pages_list = [1]
                elif spec == "last":
                    pages_list = [n]
                else:
                    pages_list = [((n + 1) // 2)]
        except Exception as e:
            await client.send_message(chat_id, f"❌ Error reading PDF: {e}")
            return
    else:
        pages_list = parse_pages_spec(spec)

    # Remove pages
    try:
        remove_pages_by_numbers(work, pruned, pages_list)
    except Exception as e:
        await client.send_message(chat_id, f"❌ Error removing pages: {e}")
        sess.pop("pdf_edit", None)
        return

    # Add banner if configured
    banner_pdf = _ensure_banner_pdf_path(uid)
    after_banner = pruned
    if banner_pdf:
        after_banner = str(user_dir / "pdfedit_bannered.pdf")
        try:
            await run_in_thread_with_timeout(
                add_banner_pages_to_pdf, pruned, after_banner, banner_pdf, place="after", timeout=BANNER_ADD_TIMEOUT
            )
        except asyncio.TimeoutError as e:
            await client.send_message(chat_id, f"❌ Error adding banner (timeout): {e}")
            sess.pop("pdf_edit", None)
            return
        except Exception as e:
            await client.send_message(chat_id, f"❌ Error adding banner: {e}")
            sess.pop("pdf_edit", None)
            return

    # Determine lock password: auto-use default if exists; otherwise proceed without locking
    default_pw = get_user_pdf_settings(uid).get("lock_password")
    if not default_pw:
        try:
            await client.send_message(chat_id, "ℹ️ No default lock password — proceeding without lock.")
        except Exception:
            pass
        await _pdf_edit_finalize_and_send(client, uid, chat_id, after_banner, None)
        return
    await _pdf_edit_finalize_and_send(client, uid, chat_id, after_banner, default_pw)


async def _pdf_edit_finalize_and_send(client, uid: int, chat_id: int, input_path: str, lock_pw: str | None):
    user_dir = get_user_temp_dir(uid)
    out_path = input_path
    if lock_pw:
        locked = str(user_dir / "pdfedit_locked.pdf")
        try:
            lock_pdf_with_password(input_path, locked, lock_pw)
            out_path = locked
        except Exception as e:
            await client.send_message(chat_id, f"❌ Error locking PDF: {e}")
            sessions.get(uid, {}).pop("pdf_edit", None)
            return
    # Multi-banner cleaning before sending (offloaded with timeout)
    try:
        raw_bytes = Path(out_path).read_bytes()
        cleaned = await run_in_thread_with_timeout(
            clean_pdf_banners, raw_bytes, uid, base_dir=BANIED_BASE_DIR, timeout=BANNER_CLEAN_TIMEOUT
        )
        if cleaned and isinstance(cleaned, (bytes, bytearray)) and cleaned != raw_bytes:
            cleaned_path = str(user_dir / "pdfedit_cleaned.pdf")
            with open(cleaned_path, "wb") as cf:
                cf.write(cleaned)
            out_path = cleaned_path
    except asyncio.TimeoutError:
        # Proceed with original out_path if cleaning times out
        pass
    except Exception:
        pass
    file_name = sessions.get(uid, {}).get("file_name") or "document.pdf"
    delay = sessions.get(uid, {}).get('delete_delay', AUTO_DELETE_DELAY)
    final_name = build_final_filename(uid, Path(file_name).name)
    await send_and_delete(client, chat_id, out_path, final_name, delay_seconds=delay)
    sessions.get(uid, {}).pop("pdf_edit", None)

async def startup_message():
    """Send startup message with bot status to all users"""
    try:
        # Get forced channels
        channels = get_forced_channels()
        force_join_text = f"@{' @'.join(channels)}" if channels else "DISABLED"
        
        # Get daily limit from config
        daily_limit_gb = 2.0  # You can make this configurable
        cooldown_seconds = 30  # You can make this configurable
        
        startup_msg = (
            "🟢 **Bot started!**\n\n"
            "PDF bot is now online and ready.\n\n"
            f"📈 Daily limit: {daily_limit_gb} GB\n"
            f"⏱️ Cooldown: {cooldown_seconds}s\n"
            "⚡️ Fast mode: ENABLED\n"
            f"📢 Force Join: {force_join_text}"
        )
        
        # Send to console/log
        print(startup_msg)
        logger.info("PDF Bot started successfully!")
        
        # Get all users from database
        all_users = []
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.execute("SELECT id FROM users")
                all_users = [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting users from DB: {e}")
            # Fallback to JSON file if DB fails
            try:
                data = _load_json(USERS_FILE, {"users": []})
                all_users = [int(u) for u in data.get("users", []) if str(u).strip()]
            except Exception as e2:
                logger.error(f"Error getting users from JSON: {e2}")
        
        # Add admin IDs if configured
        if ADMIN_IDS:
            admin_list = [int(x) for x in str(ADMIN_IDS).split(',') if x.strip()]
            for admin_id in admin_list:
                if admin_id not in all_users:
                    all_users.append(admin_id)
        
        # Remove duplicates
        all_users = list(set(all_users))
        
        # Send startup message to all users
        success_count = 0
        for user_id in all_users:
            try:
                await app.send_message(
                    user_id, 
                    startup_msg
                )
                success_count += 1
                # Small delay to avoid flood
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Failed to send startup message to user {user_id}: {e}")
                continue
        
        print(f"📢 Startup message sent to {success_count}/{len(all_users)} users")
        
    except Exception as e:
        logger.error(f"Error in startup message: {e}")

# Add startup handler
@app.on_message(filters.command("startup") & filters.user(ADMIN_IDS.split(',') if ADMIN_IDS else []))
async def manual_startup(client, message):
    """Manual startup message trigger for admins"""
    await startup_message()

# Entry point
if __name__ == "__main__":
    # Start the bot and run startup message
    app.run()