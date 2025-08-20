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
            # Ajouter le username sauvegardé à la fin
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
    
    # Nettoie les espaces multiples
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

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

sessions = {}
user_batches = defaultdict(list)  # {user_id: [docs]}
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)
cleanup_task_started = False  # Flag pour éviter de démarrer plusieurs fois la tâche

# ===== PDF user settings and helpers (banner/password/extract) =====
BANNERS_DIR = Path("banners")
BANNERS_DIR.mkdir(exist_ok=True)
PDF_SETTINGS_FILE = Path("pdf_settings.json")

# Optional dependencies
try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

from pikepdf import PasswordError


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
user_actions = defaultdict(list)  # Pour le rate limiting

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
        t for t in user_actions[user_id] 
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
    """Réinitialise tous les flags de session pour un utilisateur"""
    if user_id in sessions:
        sessions[user_id].pop('just_processed', None)
        sessions[user_id].pop('processing', None)
        sessions[user_id].pop('batch_mode', None)

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
    "pdfbot",
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

# ... (rest of the code remains the same)
        if hasattr(message, 'edit_message_text'):
            return await message.edit_message_text(text)
        # Sinon, on crée un nouveau message
        else:
            return await client.send_message(message.chat.id, text)
    except Exception as e:
        logger.error(f"Error creating/editing status: {e}")
        # En cas d'erreur, envoyer un nouveau message
        return await client.send_message(
            message.chat.id if hasattr(message, 'chat') else message.from_user.id, 
            text
        )

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
    
    user_batches[user_id].clear()
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
    user_batches[user_id].clear()
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
            [InlineKeyboardButton("🔙 Back", callback_data=f"back_settings:{user_id}")]
        ])
        await query.message.edit_text(
            "📍 Text Position\n\n"
            f"Current: <b>{current.capitalize()}</b>\n\n"
            "Examples:\n"
            "• Start: <code>@tag Document.pdf</code>\n"
            "• End: <code>Document @tag.pdf</code>",
            reply_markup=kb,
            parse_mode="html"
        )
    except Exception as e:
        logger.error(f"cb_change_position error: {e}")

@app.on_callback_query(filters.regex(r"^settings$"))
async def cb_settings(client, query: CallbackQuery):
    """Open the Settings/Parameters menu from the global Settings button."""
    try:
        user_id = query.from_user.id
        kb = build_settings_keyboard(user_id)
        await query.message.edit_text(
            "⚙️ Settings\n\nChoose what to configure:",
            reply_markup=kb
        )
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
        kb = build_pdf_actions_keyboard(user_id)
        await query.message.edit_text("Choose an action:", reply_markup=kb)
    except Exception as e:
        logger.error(f"cb_back_settings error: {e}")

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
async def remove_page_logic(client, query, user_id, page_number):
    """Common logic for deleting a page"""
    session = ensure_session_dict(user_id)
    file_id = session.get('file_id')
    if not file_id:
        await query.edit_message_text("❌ No PDF file found.")
        return
    
    file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
    
    # Check if PDF is locked
    try:
        with pikepdf.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            if page_number < 1 or page_number > total_pages:
                await query.edit_message_text(f"❌ Invalid page number. The PDF has {total_pages} pages.")
                return
            
            # Delete the page
            pdf.pages.pop(page_number - 1)
            
            # Save the modified PDF
            output_path = f"{get_user_temp_dir(user_id)}/modified_{session.get('file_name', 'document.pdf')}"
            pdf.save(output_path)
            
            # Send the modified file directly (no confirmation message)
            username = session.get('username', '')
            new_file_name = build_final_filename(user_id, session.get('file_name', 'document.pdf'))
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            
            await send_and_delete(client, query.message.chat.id, output_path, new_file_name, delay_seconds=delay)
            await query.edit_message_text(f"✅ Page {page_number} deleted successfully!")
            
            # ✅ FIX: Reset processing flag after successful page removal
            sessions[user_id]['processing'] = False
    except pikepdf.PasswordError:
        await query.edit_message_text("❌ Cannot delete page (PDF protected)")

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
    
    # Check if action is already in progress (except for clean_username, settings, and delay)
    if sessions[user_id].get('processing') and not data.startswith("clean_username") and not data.startswith("delay_") and data not in ["settings", "set_delete_delay", "back_to_start"]:
        await query.answer("⏳ Processing already in progress...", show_alert=True)
        return
    
    # Don't mark processing=True for clean_username, settings, and delay
    if not data.startswith("clean_username") and not data.startswith("cancel") and not data.startswith("delay_") and data not in ["settings", "set_delete_delay", "back_to_start"]:
        sessions[user_id]['processing'] = True
    
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
        sessions[user_id]['processing'] = False
        return
    
    # Batch clear handling
    elif data.startswith("batch_clear:"):
        user_id = int(data.split(":")[1])
        if user_id not in user_batches:
            user_batches[user_id] = []
        user_batches[user_id].clear()
        await query.edit_message_text("🧹 Batch cleared successfully!")
        sessions[user_id]['processing'] = False
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
        
        elif action == "batch_pages":
            sessions[user_id]['batch_action'] = 'pages'
            await query.edit_message_text(
                "📝 Which pages to remove from all files?\n\n"
                "Examples:\n"
                "• 1 → removes page 1\n"
                "• 1,3,5 → removes pages 1, 3 and 5\n"
                "• 1-5 → removes pages 1 to 5"
            )
        
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
            else:
                logger.error(f"❌ batch_both_first - No password found for user {user_id}")
                logger.error(f"❌ DEBUG: Session content: {session}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
        
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
                    except Exception as e:
                        logger.error(f"Error getting last page: {e}")
                        await safe_edit_message(query, "❌ Error reading PDF")
                else:
                    await safe_edit_message(query, "❌ No files in batch")
            else:
                logger.error(f"❌ batch_both_last - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
        
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
                    except Exception as e:
                        logger.error(f"Error getting middle page: {e}")
                        await safe_edit_message(query, "❌ Error reading PDF")
                else:
                    await safe_edit_message(query, "❌ No files in batch")
            else:
                logger.error(f"❌ batch_both_middle - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
        
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
            else:
                logger.error(f"❌ batch_both_manual - No password found for user {user_id}")
                await query.answer("❌ Password not found. Please restart with /process", show_alert=True)
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to process again.")
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
            "⚙️ **Settings**\n\n"
            "Configure the bot according to your needs.",
            reply_markup=keyboard
        )
        sessions[user_id]['processing'] = False
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
        sessions[user_id]['processing'] = False
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
            sessions[user_id]['processing'] = False
            return
        await process_clean_username(client, query.message, sessions[user_id])
        return
    if action == "cancel":
        sessions.pop(user_id, None)
        await query.edit_message_text("❌ Operation cancelled")
        return
    
    if user_id not in sessions:
        await query.edit_message_text("❌ Session expired. Send the PDF again.")
        sessions[user_id]['processing'] = False
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
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons
    elif action == "both":
        # Show options for "The Both" action
        both_options = InlineKeyboardMarkup([
            [InlineKeyboardButton("🪧 Add banner", callback_data=f"add_banner:{user_id}"), InlineKeyboardButton("🔐 Lock", callback_data=f"lock_now:{user_id}")],
            [InlineKeyboardButton("🛠️ Full Both Process", callback_data=f"both_full:{user_id}")],
            [InlineKeyboardButton("🗑️ Remove Pages Only", callback_data=f"both_remove_pages:{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")],
        ])
        await query.edit_message_text(
            "🛠️ **The Both** - Combined action\n\n"
            "Choose an option:\n"
            "• **Full Both Process**: Unlock + Remove pages + Clean usernames\n"
            "• **Remove Pages Only**: Just remove selected pages\n\n"
            "What do you want to do?",
            reply_markup=both_options
        )
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons
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
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons
        return
    elif action == "add_banner":
        # Add default banner immediately
        banner_pdf = _ensure_banner_pdf_path(user_id)
        if not banner_pdf:
            await query.edit_message_text("❌ No default banner. Use /setbanner first.")
            sessions[user_id]['processing'] = False
            return
        session2 = ensure_session_dict(user_id)
        file_id2 = session2.get('file_id')
        file_name2 = session2.get('file_name') or 'document.pdf'
        user_dir2 = get_user_temp_dir(user_id)
        in_path2 = await client.download_media(file_id2, file_name=user_dir2 / 'banner_input.pdf')
        out_path2 = str(user_dir2 / f"bannered_{file_name2}")
        # Show processing status
        status_msg = await client.send_message(query.message.chat.id, MESSAGES['processing'])
        try:
            add_banner_pages_to_pdf(in_path2, out_path2, banner_pdf, place='before')
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
        return
    elif action == "lock_now":
        # Lock current PDF using default password
        password = get_user_pdf_settings(user_id).get('lock_password')
        if not password:
            await query.edit_message_text("❌ No password set. Use /setpassword first.")
            sessions[user_id]['processing'] = False
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
            sessions[user_id]['processing'] = False
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
        file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_temp.pdf")
        if is_pdf_locked(file_path):
            await query.answer("❌ Cannot delete page (PDF is locked).", show_alert=True)
            return
        await delete_page_from_pdf(user_id, file_path, 0, query)
        return
    elif data.startswith("both_last:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        file_id = session.get('file_id')
        if not file_id:
            await query.answer("❌ No PDF in session.", show_alert=True)
            return
        file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_temp.pdf")
        if is_pdf_locked(file_path):
            await query.answer("❌ Cannot delete page (PDF is locked).", show_alert=True)
            return
        last_page = get_last_page_number(file_path)
        await delete_page_from_pdf(user_id, file_path, last_page, query)
        return
    elif data.startswith("both_middle:"):
        user_id = int(data.split(":")[1])
        session = ensure_session_dict(user_id)
        file_id = session.get('file_id')
        if not file_id:
            await query.answer("❌ No PDF in session.", show_alert=True)
            return
        file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_temp.pdf")
        if is_pdf_locked(file_path):
            await query.answer("❌ Cannot delete page (PDF is locked).", show_alert=True)
            return
        last_page = get_last_page_number(file_path)
        middle = last_page // 2
        await delete_page_from_pdf(user_id, file_path, middle, query)
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
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons batch
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
        if txt.startswith("/setbanner"):
            await cmd_setbanner(client, message)
            return
        if txt.startswith("/view_banner") or txt.startswith("/viewbanner"):
            await cmd_view_banner(client, message)
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
            page_num = int(page_str) - 1  # User enters 1-based, convert to 0-based
            file_id = session.get('file_id')
            if not file_id:
                await message.reply_text("❌ No PDF in session.")
            else:
                file_path = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/both_temp.pdf")
                if is_pdf_locked(file_path):
                    await message.reply_text("❌ Cannot delete page (PDF is locked).")
                else:
                    await delete_page_from_pdf(user_id, file_path, page_num, message)
        except ValueError:
            await message.reply_text("⚠️ Invalid page number.")
        session["awaiting_both_manual_page"] = False
        return

    # Gestion batch
    if session.get('awaiting_batch_password'):
        password = message.text.strip()
        await process_batch_unlock(client, message, user_id, password)
        return
    
    if session.get('batch_action') == 'pages' and 'awaiting_batch_password' not in session:
        pages_text = message.text.strip()
        await process_batch_pages(client, message, user_id, pages_text)
        return
    
    if session.get('awaiting_batch_both_password'):
        password = message.text.strip()
        logger.info(f"🔑 DEBUG: User {user_id} entered password for batch_both")
        logger.info(f"🔑 DEBUG: Password length: {len(password)}")
        
        # IMPORTANT: Ensure session exists before storing password
        session = ensure_session_dict(user_id)
        session['batch_both_password'] = password
        session['awaiting_batch_both_password'] = False
        
        # Verify immediately that password is stored correctly
        stored_password = session.get('batch_both_password', '')
        logger.info(f"🔑 DEBUG: Password stored successfully? {bool(stored_password)}")
        logger.info(f"🔑 DEBUG: Session keys after storing: {list(session.keys())}")
        
        # Log session change
        log_session_change(user_id, "SET", "batch_both_password", password)
        
        # Afficher le clavier pour la sélection des pages
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("The First", callback_data=f"batch_both_first:{user_id}"),
                InlineKeyboardButton("The Last", callback_data=f"batch_both_last:{user_id}"),
                InlineKeyboardButton("The Middle", callback_data=f"batch_both_middle:{user_id}")
            ],
            [InlineKeyboardButton("📝 Enter manually", callback_data=f"batch_both_manual:{user_id}")]
        ])
        await client.send_message(
            message.chat.id,
            "✅ Password received!\n\n"
            "**Step 2/2:** Choose pages to remove:",
            reply_markup=keyboard
        )
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons batch
        return
    
    if session.get('batch_action') == 'both' and session.get('batch_both_password'):
        pages_text = message.text.strip()
        await process_batch_both(client, message, user_id, session['batch_both_password'], pages_text)
        return
    
    # Gestion de la saisie manuelle des pages pour batch both
    if session.get('awaiting_batch_both_pages'):
        pages_text = message.text.strip()
        password = session.get('batch_both_password', '')
        if password:
            await process_batch_both(client, message, user_id, password, pages_text)
        else:
            await client.send_message(message.chat.id, "❌ Error: missing password")
        return

    # Gestion de la saisie manuelle de numéro de page pour suppression simple
    if session.get('awaiting_page_number'):
        try:
            page_number = int(message.text.strip())
            session['awaiting_page_number'] = False
            await remove_page_logic(client, message, user_id, page_number)
        except ValueError:
            await message.reply("❌ Please enter a valid number.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")
        return

    # Gestion username/hashtag (paramètre)
    if session.get('awaiting_username'):
        username = message.text.strip()
        
        # Accepter n'importe quel texte (hashtag, emoji, username, etc.)
        if username:
            session['username'] = username
            session['awaiting_username'] = False
            
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
        session.pop('awaiting_both_password', None)
        session['awaiting_both_pages'] = True
        
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
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
        
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
        # Récupérer les données nécessaires
        password = session.get('both_password', '')
        file_id = session.get('file_id')
        file_name = session.get('file_name')
        username = session.get('username', '')
        
        if not file_id or not file_name:
            await client.send_message(message.chat.id, "❌ Error: missing file data")
            sessions.pop(user_id, None)
            return
        
        # Traitement du PDF
        try:
            # Message de traitement en cours
            processing_msg = await client.send_message(message.chat.id, "⏳ Processing in progress, please wait...")
            
            file = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"both_{file_name}"
                shutil.move(file, input_path)
                
                with pikepdf.open(input_path, password=password if password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
                    total_pages = len(pdf.pages)
                    
                    pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]
                    
                    if not pages_to_keep:
                        await processing_msg.edit_text("❌ No pages remaining after removal.")
                        sessions.pop(user_id, None)
                        return
                        
                    new_pdf = pikepdf.new()
                    for page in pages_to_keep:
                        new_pdf.pages.append(page)
                        
                    new_pdf.save(output_path)
                
                # Nettoyer le nom du fichier
                cleaned_name = build_final_filename(user_id, file_name)
                
                # Envoyer le fichier AVANT le message de succès
                delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                
                # Envoyer le message de succès APRÈS le fichier
                await client.send_message(
                    message.chat.id,
                    f"✅ The Both completed successfully!\n\n"
                    f"• PDF unlocked\n"
                    f"• Pages {pages_text} removed\n"
                    f"• Usernames cleaned\n"
                    f"• Custom username added: {username if username else 'None'}"
                )
                
                # Supprimer le message de traitement
                try:
                    await processing_msg.delete()
                except:
                    pass
                        
        except Exception as e:
            logger.error(f"Error process_both_final: {e}")
            try:
                await processing_msg.edit_text("❌ Error processing PDF")
            except:
                await client.send_message(message.chat.id, "❌ Error processing PDF")
        finally:
            # Nettoyer tout à la fin
            user_batches[user_id].clear()
            session = ensure_session_dict(user_id)
            session.pop('batch_mode', None)
            session.pop('batch_action', None)
            session.pop('awaiting_batch_password', None)
            session['processing'] = False
            # IMPORTANT: Supprimer complètement la session
            sessions.pop(user_id, None)
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
        sessions[user_id]['processing'] = False
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
        user_batches[user_id].clear()
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('awaiting_batch_password', None)
        session['processing'] = False

async def process_batch_pages(client, message, user_id, pages_text):
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        sessions[user_id]['processing'] = False
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
        sessions[user_id]['processing'] = False
        return
    
    # Récupérer le username depuis la session
    session = ensure_session_dict(user_id)
    username = session.get('username', '')
    
    logger.info(f"🔍 Start process_batch_pages - User {user_id} - Time: {datetime.now()}")
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
        user_batches[user_id].clear()
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session['processing'] = False

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
        sessions[user_id]['processing'] = False
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
        sessions[user_id]['processing'] = False
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
                    
                    new_file_name = build_final_filename(user_id, file_info['file_name'])
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                    
                    success_count += 1
                    
            except Exception as e:
                logger.error(f"Error batch both file {i}: {e}")
                error_count += 1
        
        await status.edit_text(
            f"✅ Combined processing complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )
        
    finally:
        user_batches[user_id].clear()
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('batch_both_password', None)
        session.pop('awaiting_batch_both_pages', None)
        session['processing'] = False

# Modification des fonctions existantes pour utiliser send_and_delete
async def process_unlock(client, message, session, password):
    user_id = message.from_user.id
    
    try:
        await message.delete()
    except:
        pass
    
    logger.info(f"🔓 process_unlock - User {user_id} - Password length: {len(password)}")
    
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    
    try:
        # Vérifier l'existence du fichier dans la session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return
        
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"unlocked_{session['file_name']}"
            shutil.move(file, input_path)
            
            with pikepdf.open(input_path, password=password if password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
                pdf.save(output_path)
            
            # Nettoyer le nom du fichier
            username = session.get('username', '')
            cleaned_name = build_final_filename(user_id, session['file_name'])
            
            # Supprimer le message de statut
            try:
                await status.delete()
            except:
                pass
            
            # Envoyer le message de succès
            await client.send_message(
                message.chat.id,
                "✅ PDF unlocked successfully!"
            )
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                    
    except Exception as e:
        logger.error(f"Error process_unlock: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

async def process_clean_username(client, message_or_query, session):
    """Fonction pour nettoyer uniquement les usernames du nom de fichier"""
    user_id = message_or_query.from_user.id if hasattr(message_or_query, 'from_user') else message_or_query.chat.id
    
    try:
        # Supprimer le message de l'utilisateur si possible
        try:
            await message_or_query.delete()
        except:
            pass
        
        logger.info(f"🧹 process_clean_username - User {user_id}")
        
        status = await client.send_message(message_or_query.chat.id, MESSAGES['processing'])
        
        # Vérifier l'existence du fichier dans la session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a file first.")
            return
        
        file_name = session['file_name']
        
        # Traitement différent selon le type de fichier
        if is_pdf_file(file_name):
            # Traitement PDF (existant)
            file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"cleaned_{file_name}"
                shutil.move(file, input_path)
                
                with pikepdf.open(input_path, allow_overwriting_input=True) as pdf:
                    pdf.save(output_path)
                
                # Nettoyer le nom du fichier
                username = session.get('username', '')
                cleaned_name = build_final_filename(user_id, file_name)
                
                # Supprimer le message de statut
                try:
                    await status.delete()
                except:
                    pass
                
                # Envoyer le message de succès
                await client.send_message(
                    message_or_query.chat.id,
                    "✅ Usernames cleaned in filename!"
                )
                
                # Envoyer le fichier
                delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(client, message_or_query.chat.id, output_path, cleaned_name, delay_seconds=delay)
        
        else:
            # Traitement vidéo (simple renommage)
            file_path = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/{file_name}")
            
            # Nettoyer le nom du fichier
            username = session.get('username', '')
            cleaned_name = build_final_filename(user_id, file_name)
            
            # Créer le nouveau chemin avec le nom nettoyé
            new_path = os.path.join(get_user_temp_dir(user_id), cleaned_name)
            shutil.move(file_path, new_path)
            
            # Supprimer le message de statut
            try:
                await status.delete()
            except:
                pass
            
            # Envoyer le message de succès
            await client.send_message(
                message_or_query.chat.id,
                "✅ Usernames cleaned in filename!"
            )
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message_or_query.chat.id, new_path, cleaned_name, delay_seconds=delay)
                    
    except Exception as e:
        logger.error(f"Error process_clean_username: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

async def process_batch_clean(client, message, user_id):
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    # Filtrer uniquement les PDF
    pdf_files = [f for f in files if is_pdf_file(f['file_name'])]
    
    if not pdf_files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No PDF files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No PDF files in batch")
        sessions[user_id]['processing'] = False
        return
    
    status = await create_or_edit_status(client, message, f"🧹 Cleaning usernames on {len(pdf_files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(pdf_files):
            try:
                await status.edit_text(f"🧹 Cleaning file {i+1}/{len(pdf_files)}...")
                
                file_name = file_info['file_name']
                
                # Traitement PDF (seulement PDF ici)

                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"cleaned_{file_name}"
                    shutil.move(file, input_path)
                    with pikepdf.open(input_path, allow_overwriting_input=True) as pdf:
                        pdf.save(output_path)
                username = sessions[user_id].get('username', '')
                cleaned_name = build_final_filename(user_id, file_name)
                delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)

                success_count += 1

            except Exception as e:
                logger.error(f"Error batch clean file {i}: {e}")
                error_count += 1

        await status.edit_message_text(
            f"✅ Cleaning complete!\n\n"
            f"Successful: {success_count}\n"
            f"Errors: {error_count}"
        )

    finally:
        user_batches[user_id].clear()
        sessions[user_id].pop('batch_mode', None)
        sessions[user_id]['processing'] = False

async def process_video_batch_item(client, message, file_info, user_id):
    """Traite une vidéo en mode batch clean - SANS TÉLÉCHARGEMENT"""
    try:
        file_id = file_info['file_id']
        caption = file_info.get('caption', '')  # Caption originale
        
        # Utiliser la fonction unifiée pour nettoyer la caption
        final_caption = clean_caption_with_username(caption, user_id)
        
        # Envoyer directement avec le file_id (pas de téléchargement!)
        await client.send_video(
            chat_id=message.chat.id,
            video=file_id,  # Utilise le file_id original
            caption=final_caption
        )
        
        # Petit délai anti-flood
        await asyncio.sleep(0.7)
        
        return True
        
    except Exception as e:
        logger.error(f"Error processing video batch: {e}")
        return False

async def process_pdf_batch_item(client, message, file_info, user_id):
    """Traite un PDF en mode batch clean"""
    try:
        file_name = file_info['file_name']
        local_path = file_info.get('local_path')
        
        if local_path and os.path.exists(local_path):
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"cleaned_{file_name}"
                shutil.copy2(local_path, input_path)
                
                with pikepdf.open(input_path, allow_overwriting_input=True) as pdf:
                    pdf.save(output_path)
                
                # Nettoyer le nom du fichier
                username = sessions[user_id].get('username', '')
                cleaned_name = build_final_filename(user_id, file_name)
                
                delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
        
            return True
        else:
            logger.error(f"Local PDF file not found: {local_path}")
            return False
            
    except Exception as e:
        logger.error(f"Error processing PDF batch: {e}")
        return False

async def clean_and_send_video(client, chat_id, file_id, caption, user_id, delay=AUTO_DELETE_DELAY):
    """Fonction unifiée pour nettoyer et envoyer une vidéo"""
    try:
        # Nettoyer la caption et ajouter le username
        final_caption = clean_caption_with_username(caption, user_id)
                    
        # Marquer qu'on traite un fichier
        await set_just_processed_flag(user_id)
        
        # Envoyer la vidéo
        sent = await client.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=final_caption
        )
        
        # Planifier la suppression si nécessaire
        if delay > 0:
            async def delete_after_delay():
                await asyncio.sleep(delay)
                try:
                    await sent.delete()
                except Exception as e:
                    logger.error(f"Error deleting video: {e}")
            
            asyncio.create_task(delete_after_delay())
        
        # Le flag est géré par set_just_processed_flag
        pass
        
        return True
        
    except Exception as e:
        logger.error(f"Error in clean_and_send_video: {e}")
        return False

async def process_batch_generic(client, message, user_id, action_func, action_name):
    """Template générique pour le traitement batch"""
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    if not files:
        await safe_edit_message(message, "❌ No files in batch")
        sessions[user_id]['processing'] = False
        return
    
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"⏳ {action_name} {i+1}/{len(files)}...")
                result = await action_func(client, file_info, user_id, i)
                if result:
                    success_count += 1
                else:
                    error_count += 1
            except Exception as e:
                logger.error(f"Error in batch {action_name}: {e}")
                error_count += 1
        
        await show_batch_results(status, success_count, error_count)
        
    finally:
        user_batches[user_id].clear()
        reset_session_flags(user_id)
        sessions[user_id]['processing'] = False

async def show_batch_results(status, success_count, error_count):
    """Affiche les résultats du traitement batch"""
    if error_count == 0:
        await status.edit_message_text(
            f"✅ **Batch processing completed successfully!**\n\n"
            f"📊 **Statistics:**\n"
            f"• ✅ Success: {success_count} files\n"
            f"• ❌ Errors: {error_count} files\n\n"
            f"🎉 All files have been processed and cleaned!"
        )
    else:
        await status.edit_message_text(
            f"⚠️ **Batch processing completed with errors**\n\n"
            f"📊 **Statistics:**\n"
            f"• ✅ Success: {success_count} files\n"
            f"• ❌ Errors: {error_count} files\n\n"
            f"💡 Some files could not be processed. Please check the error messages above."
        )

async def handle_session_expired_error(client, chat_id, file_type="file"):
    """Gère les erreurs de session expirée avec un message clair"""
    await client.send_message(
        chat_id,
        f"❌ **{file_type.title()} session expired**\n\n"
        f"The {file_type} session has expired and cannot be processed.\n"
        f"Please resend the {file_type} - it must be processed within 1-2 minutes of sending.\n\n"
        f"This happens when {file_type}s are not processed quickly enough after being sent."
    )

async def cleanup_batch_temp_files(user_id):
    """Nettoie automatiquement tous les fichiers temporaires d'un utilisateur en mode batch"""
    try:
        # Nettoyer les fichiers temporaires générés pendant le traitement
        temp_dir = get_user_temp_dir(user_id)
        if os.path.exists(temp_dir):
            for filename in os.listdir(temp_dir):
                if filename.startswith('batch_') or filename.startswith('temp_'):
                    file_path = os.path.join(temp_dir, filename)
                    try:
                        os.remove(file_path)
                        logger.info(f"Cleaned up temporary file: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting temporary file {file_path}: {e}")
                        
    except Exception as e:
        logger.error(f"Error in cleanup_batch_temp_files for user {user_id}: {e}")

# Fonction de nettoyage périodique des fichiers temporaires
async def cleanup_temp_files():
    """Nettoie les fichiers temporaires plus anciens que 1 heure"""
    while True:
        try:
            current_time = datetime.now()
            # Parcourir tous les sous-dossiers utilisateur
            for user_dir in TEMP_DIR.iterdir():
                if user_dir.is_dir():
                    for file_path in user_dir.glob("*"):
                        if file_path.is_file():
                            file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                            if current_time - file_time > timedelta(hours=1):
                                file_path.unlink()
                                logger.info(f"Temporary file deleted: {file_path}")
                    
                    # Supprimer le dossier utilisateur s'il est vide
                    if not any(user_dir.iterdir()):
                        user_dir.rmdir()
                        logger.info(f"Empty user directory deleted: {user_dir}")
            
            # Nettoyer les flags processing bloqués
            for user_id in list(sessions.keys()):
                if sessions[user_id].get('processing'):
                    # Vérifier si le flag est bloqué depuis trop longtemps
                    last_activity = sessions[user_id].get('last_activity')
                    if last_activity and (current_time - last_activity) > timedelta(minutes=5):
                        logger.info(f"Libération du flag processing bloqué pour user {user_id}")
                        sessions[user_id]['processing'] = False
                
                # Nettoyer les sessions inactives depuis plus de 10 minutes
                last_activity = sessions[user_id].get('last_activity')
                if last_activity and (current_time - last_activity) > timedelta(minutes=10):
                    logger.info(f"🗑️ Session expirée pour user {user_id}")
                    sessions.pop(user_id, None)
                    # Nettoyer aussi les batches
                    if user_id in user_batches:
                        user_batches[user_id].clear()
                        
        except Exception as e:
            logger.error(f"Error cleaning temp files: {e}")
        
        await asyncio.sleep(3600)  # Vérifier toutes les heures

# 🔧 FONCTIONS UTILITAIRES PARTAGÉES 🔧
async def process_single_pdf(input_path, output_path, password=None, pages_to_remove=None, username=""):
    """
    Fonction utilitaire pour traiter un PDF unique
    Retourne: (success, error_message)
    """
    try:
        with pikepdf.open(input_path, password=password if password and password.lower() != 'none' else '', allow_overwriting_input=True) as pdf:
            # Suppression de pages si demandée
            if pages_to_remove:
                total_pages = len(pdf.pages)
                invalid_pages = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid_pages:
                    return False, f"Invalid pages: {invalid_pages} (PDF has {total_pages} pages)"
                
                pages_to_keep = [p for i, p in enumerate(pdf.pages) if (i + 1) not in pages_to_remove]
                
                if not pages_to_keep:
                    return False, "No pages remaining after removal"
                
                new_pdf = pikepdf.new()
                for page in pages_to_keep:
                    new_pdf.pages.append(page)
                
                new_pdf.save(output_path)
            else:
                # Pas de suppression, sauvegarder directement
                pdf.save(output_path)
            
            return True, None
            
    except pikepdf.PasswordError:
        return False, "Incorrect password"
    except Exception as e:
        return False, f"PDF processing error: {str(e)}"

async def process_batch_pdfs(file_list, password=None, pages_to_remove=None, username="", temp_dir=None):
    """
    Fonction utilitaire pour traiter une liste de PDFs
    Retourne: (success_count, error_count, results)
    """
    success_count = 0
    error_count = 0
    results = []
    
    for i, file_info in enumerate(file_list):
        try:
            # Télécharger le fichier
            file_path = await client.download_media(
                file_info['file_id'], 
                file_name=f"{temp_dir}/batch_{i}.pdf"
            )
            
            # Traiter le PDF
            input_path = Path(file_path)
            output_path = Path(temp_dir) / f"processed_{file_info['file_name']}"
            
            success, error_msg = await process_single_pdf(
                input_path, output_path, password, pages_to_remove, username
            )
            
            if success:
                success_count += 1
                results.append({
                    'success': True,
                    'input_path': input_path,
                    'output_path': output_path,
                    'file_name': file_info['file_name']
                })
            else:
                error_count += 1
                results.append({
                    'success': False,
                    'file_name': file_info['file_name'],
                    'error': error_msg
                })
                
        except Exception as e:
            error_count += 1
            results.append({
                'success': False,
                'file_name': file_info.get('file_name', f'file_{i}'),
                'error': f"Download/processing error: {str(e)}"
            })
    
    return success_count, error_count, results

def parse_pages_text(pages_text):
    """
    Parse le texte des pages (ex: "1,3,5" ou "1-5")
    Retourne: (pages_set, error_message)
    """
    try:
        pages_to_remove = set()
        for part in pages_text.replace(' ', '').split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                pages_to_remove.update(range(start, end + 1))
            else:
                pages_to_remove.add(int(part))
        return pages_to_remove, None
    except ValueError:
        return None, "Invalid format. Use: 1,3,5 or 1-5"

def ensure_session_dict(user_id):
    """
    S'assure que la session d'un utilisateur est un dictionnaire
    Retourne la session (dict)
    """
    if user_id not in sessions:
        sessions[user_id] = {}
        return sessions[user_id]
    
    session = sessions[user_id]
    
    if not isinstance(session, dict):
        logger.warning(f"⚠️ Session corrompue pour user {user_id}: {type(session)} = {session}")
        sessions[user_id] = {}
        return sessions[user_id]
    
    return session

def safe_session_set(user_id, key, value):
    """
    Définit une valeur dans la session de manière sûre
    """
    session = ensure_session_dict(user_id)
    session[key] = value
    logger.debug(f"🔧 Session {user_id}[{key}] = {value}")

def safe_session_get(user_id, key, default=None):
    """
    Récupère une valeur de la session de manière sûre
    """
    session = ensure_session_dict(user_id)
    return session.get(key, default)

def log_session_change(user_id, operation, key=None, value=None):
    """Log les changements dans les sessions pour le débogage"""
    session = sessions.get(user_id, {})
    logger.info(f"📊 SESSION CHANGE - User: {user_id}")
    logger.info(f"📊 Operation: {operation}")
    if key:
        logger.info(f"📊 Key: {key}")
    if value is not None:
        logger.info(f"📊 Value type: {type(value).__name__}")
        if isinstance(value, str):
            logger.info(f"📊 Value length: {len(value)}")
    logger.info(f"📊 Current session keys: {list(session.keys())}")
    logger.info(f"📊 Has batch_both_password: {'batch_both_password' in session}")

# 🔧 EXEMPLE DE REFACTORISATION AVEC LES FONCTIONS SÛRES 🔧
# 
# AU LIEU DE :
# sessions[user_id]['processing'] = True
# sessions[user_id]['username'] = saved_username
# 
# UTILISER :
# safe_session_set(user_id, 'processing', True)
# safe_session_set(user_id, 'username', saved_username)
# 
# AU LIEU DE :
# if sessions[user_id].get('username'):
#     username = sessions[user_id]['username']
# 
# UTILISER :
# username = safe_session_get(user_id, 'username')
# if username:
#     # utiliser username

# 🔧 SYSTÈME D'ÉTATS STRUCTURÉ 🔧
from enum import Enum

class UserState(Enum):
    IDLE = "idle"
    AWAITING_USERNAME = "awaiting_username"
    AWAITING_PASSWORD = "awaiting_password"
    AWAITING_PAGES = "awaiting_pages"
    AWAITING_BOTH_PASSWORD = "awaiting_both_password"
    AWAITING_BOTH_PAGES = "awaiting_both_pages"
    AWAITING_BATCH_PASSWORD = "awaiting_batch_password"
    AWAITING_BATCH_BOTH_PASSWORD = "awaiting_batch_both_password"
    AWAITING_BATCH_BOTH_PAGES = "awaiting_batch_both_pages"
    PROCESSING = "processing"

def set_user_state(user_id, state, **kwargs):
    """Définit l'état d'un utilisateur avec des données supplémentaires"""
    session = ensure_session_dict(user_id)
    
    session['state'] = state.value
    session['state_data'] = kwargs
    session['last_activity'] = datetime.now()
    
    logger.info(f"🔧 User {user_id} state changed to: {state.value}")

def get_user_state(user_id):
    """Récupère l'état actuel d'un utilisateur"""
    if user_id not in sessions:
        return UserState.IDLE, {}
    
    session = ensure_session_dict(user_id)
    state_name = session.get('state', 'idle')
    state_data = session.get('state_data', {})
    
    try:
        return UserState(state_name), state_data
    except ValueError:
        return UserState.IDLE, {}

def clear_user_state(user_id):
    """Remet l'utilisateur à l'état IDLE"""
    session = ensure_session_dict(user_id)
    session['state'] = UserState.IDLE.value
    session['state_data'] = {}
    logger.info(f"🔧 User {user_id} state cleared to IDLE")

# 🔧 EXEMPLE DE REFACTORISATION AVEC LE NOUVEAU SYSTÈME 🔧
async def handle_text_with_states(client, message: Message):
    """
    Exemple de handler refactorisé utilisant le système d'états
    """
    user_id = message.from_user.id
    state, state_data = get_user_state(user_id)
    
    logger.info(f"🔧 Handling text for user {user_id} in state: {state.value}")
    
    if state == UserState.IDLE:
        # Aucune action en cours, ignorer
        return
    
    elif state == UserState.AWAITING_USERNAME:
        # Gestion de l'ajout de hashtag/tag
        username = message.text.strip()
        
        # Accepter n'importe quel texte (hashtag, emoji, username, etc.)
        if username:
            if save_username(user_id, username):
                await client.send_message(message.chat.id, f"✅ Tag saved: {username}")
            else:
                await client.send_message(message.chat.id, f"✅ Tag saved in session: {username}\n⚠️ (Could not save to file)")
            clear_user_state(user_id)
        else:
            await client.send_message(message.chat.id, "❌ Please send some text to use as your tag.")
    
    elif state == UserState.AWAITING_PASSWORD:
        # Gestion du mot de passe pour déverrouillage
        password = message.text.strip()
        file_id = state_data.get('file_id')
        file_name = state_data.get('file_name')
        
        if file_id and file_name:
            # Traiter le déverrouillage
            await process_unlock_with_state(client, message, user_id, file_id, file_name, password)
        else:
            await client.send_message(message.chat.id, "❌ Error: missing file data")
            clear_user_state(user_id)
    
    elif state == UserState.AWAITING_PAGES:
        # Gestion de la saisie manuelle des pages
        pages_text = message.text.strip()
        pages_to_remove, error = parse_pages_text(pages_text)
        
        if error:
            await client.send_message(message.chat.id, f"❌ {error}")
            return
        
        file_id = state_data.get('file_id')
        file_name = state_data.get('file_name')
        
        if file_id and file_name:
            await process_pages_with_state(client, message, user_id, file_id, file_name, pages_to_remove)
        else:
            await client.send_message(message.chat.id, "❌ Error: missing file data")
            clear_user_state(user_id)

# Utility function for deleting a page from a PDF by index
async def delete_page_from_pdf(user_id, file_path, page_index, reply_context):
    try:
        with pikepdf.open(file_path, allow_overwriting_input=True) as pdf:
            if page_index < 0 or page_index >= len(pdf.pages):
                await reply_context.reply_text("❌ Invalid page.")
                return
            pdf.pages.pop(page_index)
            pdf.save(file_path)
        await reply_context.reply_document(document=file_path, caption=f"✅ Page {page_index + 1} deleted.")
    except Exception as e:
        logger.error(f"Error deleting page: {e}")
        await reply_context.reply_text("❌ An error occurred.")

# ✅ 4. Fonction is_pdf_locked(file_path)
def is_pdf_locked(file_path: str) -> bool:
    try:
        with pikepdf.open(file_path) as pdf:
            return False  # Si on peut l'ouvrir sans mot de passe, il n'est pas verrouillé
    except pikepdf.PasswordError:
        return True  # Verrouillé
    except Exception as e:
        logger.error(f"Error is_pdf_locked: {e}")
        return True

# ✅ 5. Fonction get_last_page_number(file_path)
def get_last_page_number(file_path: str) -> int:
    try:
        with pikepdf.open(file_path, allow_overwriting_input=True) as pdf:
            return len(pdf.pages) - 1
    except Exception as e:
        logger.error(f"Error getting last page: {e}")
        return 0

# ✅ 6. Fonction pour créer le menu de suppression de pages
def get_remove_pages_buttons(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("The First", callback_data=f"both_first:{user_id}"),
            InlineKeyboardButton("The Last", callback_data=f"both_last:{user_id}"),
        ],
        [
            InlineKeyboardButton("The Middle", callback_data=f"both_middle:{user_id}"),
            InlineKeyboardButton("Enter Manually", callback_data=f"both_manual:{user_id}"),
        ]
    ])

# Cette fonction a été supprimée car la logique est maintenant intégrée directement dans handle_all_text

async def process_pages(client, message, session, pages_text):
    """Traite la suppression de pages sans mot de passe"""
    user_id = message.from_user.id
    
    try:
        # Supprimer le message de l'utilisateur
        try:
            await message.delete()
        except:
            pass
        
        logger.info(f"🗑️ process_pages - User {user_id}")
        
        status = await client.send_message(message.chat.id, MESSAGES['processing'])
        
        # Vérifier l'existence du fichier dans la session
        if 'file_id' not in session:
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return
        
        # Parser les pages
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
            except pikepdf.PasswordError:
                await status.edit_text("❌ PDF is protected. Please use 'Unlock' first or provide a password.")
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
                f"✅ Pages {pages_text} removed successfully!"
            )
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                    
    except Exception as e:
        logger.error(f"Error process_pages: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
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
    await client.send_message(message.chat.id, "🖼️ Send me your banner (image or 1-page PDF).")


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
        await client.send_message(message.chat.id, "ℹ️ No banner set. Use /setbanner")
        return
    if bp.lower().endswith(".pdf"):
        await client.send_document(message.chat.id, bp, caption="📄 Banner (PDF)")
    else:
        await client.send_photo(message.chat.id, bp, caption="🖼️ Banner (image)")


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
    if sessions.get(uid, {}).get("awaiting_banner_upload"):
        path = await client.download_media(message.photo.file_id, file_name=BANNERS_DIR / f"banner_{uid}.jpg")
        update_user_pdf_settings(uid, banner_path=str(path))
        sessions[uid]["awaiting_banner_upload"] = False
        await client.send_message(message.chat.id, "✅ Banner image saved.")


# Hook inside existing document handler flow to accept banner file uploads
@app.on_message(filters.document & filters.private)
async def on_document_maybe_banner_or_pdf_forward(client, message: Message):
    uid = message.from_user.id
    # Force-join check
    if not await is_user_in_channel(uid):
        await send_force_join_message(client, message)
        return
    if sessions.get(uid, {}).get("awaiting_banner_upload"):
        doc = message.document
        name = (doc.file_name or "banner")
        if (doc.mime_type or "").startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")):
            path = await client.download_media(doc.file_id, file_name=BANNERS_DIR / f"banner_{uid}{Path(name).suffix}")
            update_user_pdf_settings(uid, banner_path=str(path))
            sessions[uid]["awaiting_banner_upload"] = False
            await client.send_message(message.chat.id, "✅ Banner image saved.")
            return
        elif name.lower().endswith(".pdf"):
            path = await client.download_media(doc.file_id, file_name=BANNERS_DIR / f"banner_{uid}.pdf")
            update_user_pdf_settings(uid, banner_path=str(path))
            sessions[uid]["awaiting_banner_upload"] = False
            await client.send_message(message.chat.id, "✅ Banner PDF saved.")
            return
    # If not awaiting banner, fall through to original document flow handled elsewhere


# ===== Inline buttons additions: Add banner + Lock =====

def build_pdf_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clean usernames", callback_data=f"clean_username:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
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
        await client.send_message(chat_id, "🧹 Pages to remove? e.g. 1,3,5-7 — send `none` to skip.")
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
        await client.send_message(chat_id, "🧹 Pages to remove? e.g. 1,3,5-7 — send `none` to skip.")
        return

    if sess.get("awaiting_pdf_edit_pages"):
        sess["awaiting_pdf_edit_pages"] = False
        pages = parse_pages_spec(message.text)
        user_dir = get_user_temp_dir(uid)
        cur = sess["pdf_edit"]["work"]
        pruned = str(user_dir / "pdfedit_pruned.pdf")
        try:
            remove_pages_by_numbers(cur, pruned, pages)
        except Exception as e:
            await client.send_message(chat_id, f"❌ Error removing pages: {e}")
            sess.pop("pdf_edit", None)
            return
        banner_pdf = _ensure_banner_pdf_path(uid)
        after_banner = pruned
        if banner_pdf:
            after_banner = str(user_dir / "pdfedit_bannered.pdf")
            try:
                add_banner_pages_to_pdf(pruned, after_banner, banner_pdf, place="before")
            except Exception as e:
                await client.send_message(chat_id, f"❌ Error adding banner: {e}")
                sess.pop("pdf_edit", None)
                return
        default_pw = get_user_pdf_settings(uid).get("lock_password")
        if not default_pw:
            sess["pdf_edit"]["work"] = after_banner
            sess["awaiting_pdf_edit_lock_password"] = True
            await client.send_message(chat_id, "🔐 Send password to lock PDF (or `skip` to send without lock).")
            return
        await _pdf_edit_finalize_and_send(client, uid, chat_id, after_banner, default_pw)
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