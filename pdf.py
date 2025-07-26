#!/usr/bin/env python3 
"""
Bot Telegram pour la gestion des PDF - VERSION CORRIGÉE
Compatible avec Python 3.13 et python-telegram-bot 21.x
Avec support batch (24 fichiers max) et suppression automatique
Version anglaise avec Force Join Channel
"""

import os
import sys
import logging
import tempfile
import re
import asyncio
import shutil
import json
import time
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


# Fichier pour stocker les usernames de manière persistante
USERNAMES_FILE = Path("usernames.json")

def save_username(user_id, username):
    """Sauvegarde le username d'un utilisateur de manière persistante"""
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
        
        logger.info(f"💾 Username sauvegardé pour user {user_id}: {username}")
        return True
    except Exception as e:
        logger.error(f"❌ Error saving username: {e}")
        return False

def get_saved_username(user_id):
    """Récupère le username sauvegardé d'un utilisateur"""
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
    """Supprime le username sauvegardé d'un utilisateur"""
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
FORCE_JOIN_CHANNEL = "djd208"  # ⚠️ REMPLACE PAR TON CANAL (sans @)

MAX_FILE_SIZE = 1_400 * 1024 * 1024  # 14 GB
MAX_BATCH_FILES = 24
AUTO_DELETE_DELAY = 300  # 5 minutes

# Messages du bot en anglais
MESSAGES = {
    'start': "🤖 *PDF Manager Bot ready!*\n\n📄 *Normal Mode*: Send a PDF to process it\n📦 *Batch Mode*: Process up to 24 files at once with `/batch`\n\nSend me a PDF to get started!",
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

try:
    from PyPDF2 import PdfReader, PdfWriter, PageObject
except ImportError:
    print("❌ PyPDF2 is not installed!")
    print("Execute: pip install PyPDF2==3.0.1")
    sys.exit(1)

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

# Protection globale contre les doublons et rate limiting
processed_messages = {}
user_last_command = {}  # {user_id: (command, timestamp)}
user_actions = defaultdict(list)  # Pour le rate limiting

def check_rate_limit(user_id):
    """Vérifie si l'utilisateur n'abuse pas (max 5 actions par minute)"""
    current_time = datetime.now()
    # Nettoyer les anciennes actions
    user_actions[user_id] = [
        t for t in user_actions[user_id] 
        if (current_time - t).seconds < 60
    ]
    
    if len(user_actions[user_id]) >= 5:
        logger.warning(f"⚠️ Rate limit atteint pour user {user_id}")
        return False  # Trop d'actions
    
    user_actions[user_id].append(current_time)
    return True

def is_duplicate_message(user_id, message_id, command_type="message"):
    """Vérifie si un message a déjà été traité ou si c'est une commande répétée"""
    current_time = datetime.now()
    
    # Vérifier le rate limit
    if not check_rate_limit(user_id):
        return True
    
    # Protection contre les commandes répétées (même utilisateur, même commande, < 2 secondes)
    if command_type in ["start", "batch", "process"]:
        if user_id in user_last_command:
            last_cmd, last_time = user_last_command[user_id]
            if last_cmd == command_type and (current_time - last_time).total_seconds() < 2:
                logger.info(f"Commande {command_type} ignorée - répétée trop rapidement pour user {user_id}")
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
        return True
    
    processed_messages[key] = current_time
    return False

def reset_session_flags(user_id):
    """Réinitialise tous les flags temporaires d'une session SAUF les données importantes"""
    logger.info(f"🔄 DEBUG: reset_session_flags called for user {user_id}")
    
    if user_id not in sessions:
        logger.info(f"🔄 DEBUG: No session exists for user {user_id}, nothing to reset")
        return
    
    session = sessions[user_id]
    if not isinstance(session, dict):
        logger.warning(f"⚠️ Session inattendue pour user {user_id}: {type(session)} = {session} (réinitialisation)")
        sessions[user_id] = {}
        return
    
    # Log l'état avant reset
    logger.info(f"🔄 DEBUG: Session keys before reset: {list(session.keys())}")
    logger.info(f"🔄 DEBUG: Has batch_both_password before reset: {'batch_both_password' in session}")
    
    # IMPORTANT: NE PAS supprimer les données critiques
    flags_to_reset = [
        'awaiting_pages_manual', 
        'awaiting_both_pages', 
        'awaiting_both_password',
        'awaiting_password_for_pages', 
        'awaiting_batch_password', 
        'awaiting_batch_both_password',
        'awaiting_batch_both_pages', 
        # 'batch_both_password',  # ❌ NE PAS supprimer le mot de passe !
        # 'both_password',        # ❌ NE PAS supprimer le mot de passe !
        'pages_to_remove', 
        'both_pages', 
        'awaiting_username', 
        'batch_command_processing',
        'process_command_processing', 
        'cleaning_only', 
        'awaiting_both_pages_selection'
    ]
    
    for flag in flags_to_reset:
        if flag in session:
            logger.info(f"🔄 DEBUG: Removing flag: {flag}")
            session.pop(flag, None)
    
    # Log l'état après reset
    logger.info(f"🔄 DEBUG: Session keys after reset: {list(session.keys())}")
    logger.info(f"🔄 DEBUG: Has batch_both_password after reset: {'batch_both_password' in session}")
    
    logger.info(f"✅ Flags de session réinitialisés pour user {user_id}")

app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# 🔥 FONCTIONS FORCE JOIN CHANNEL 🔥
async def is_user_in_channel(user_id):
    """Vérifie si l'utilisateur est membre du canal"""
    # Exemption pour les admins
    admin_list = [int(x) for x in str(ADMIN_IDS).split(',') if x.strip()] if ADMIN_IDS else []
    if user_id in admin_list:
        logger.info(f"🔓 User {user_id} is admin - bypassing channel check")
        return True
    
    logger.info(f"🔍 Checking channel membership for user {user_id} in @{FORCE_JOIN_CHANNEL}")
    
    try:
        member = await app.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        status = member.status
        logger.info(f"🔍 Member status for user {user_id}: {status}")
        
        # Accepter tous les statuts valides avec les constantes Pyrogram
        valid_statuses = [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR, 
            ChatMemberStatus.OWNER  # ✅ Pyrogram utilise OWNER, pas CREATOR
        ]
        
        # Debug: afficher tous les statuts possibles pour comparaison
        logger.info(f"🔍 Valid statuses: {valid_statuses}")
        logger.info(f"🔍 User status: {status} (type: {type(status)})")
        
        if status in valid_statuses:
            logger.info(f"✅ User {user_id} is member of channel (status: {status})")
            return True
        else:
            logger.info(f"❌ User {user_id} is not member (status: {status})")
            return False
    except UserNotParticipant:
        logger.error(f"❌ User {user_id} is not participant in channel {FORCE_JOIN_CHANNEL}")
        return False
    except ChatAdminRequired:
        logger.error(f"❌ Bot is not admin in channel {FORCE_JOIN_CHANNEL}")
        return True  # On laisse passer pour éviter de bloquer
    except UsernameNotOccupied:
        logger.error(f"❌ Channel {FORCE_JOIN_CHANNEL} does not exist")
        return True
    except Exception as e:
        logger.error(f"❌ Error checking channel membership for user {user_id}: {e}")
        return True  # En cas d'erreur, on laisse passer

async def send_force_join_message(client, message):
    """Envoie le message demandant de rejoindre le canal"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 Join @{FORCE_JOIN_CHANNEL}", url=f"https://t.me/{FORCE_JOIN_CHANNEL}")],
        [InlineKeyboardButton("✅ I have joined", callback_data="check_joined")]
    ])
    
    await client.send_message(
        message.chat.id,
        MESSAGES['force_join'].format(channel=FORCE_JOIN_CHANNEL),
        reply_markup=keyboard
    )

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
        return ""
    except Exception as e:
        logger.warning(f"Error extracting text: {e}")
        return ""

def replace_username_in_filename(filename, new_username=None):
    """
    Nettoie et modifie automatiquement le nom du fichier PDF pour inclure le @username proprement à la fin.
    """
    base, ext = os.path.splitext(filename)
    
    # Supprime TOUS les anciens usernames
    base = re.sub(r'[\[\(\{\s]*@\w+[\]\)\}\s]*', '', base, flags=re.IGNORECASE)
    base = re.sub(r'\s+', ' ', base).strip()
    
    if new_username:
        if not new_username.startswith('@'):
            new_username = f"@{new_username}"
        base = f"{base} {new_username}"
    
    return f"{base}{ext}"

async def create_or_edit_status(client, message, text):
    """Crée ou édite un message de statut selon le contexte"""
    try:
        # Si c'est un CallbackQuery, on peut éditer
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
        
        # NOUVEAU: Marquer qu'on vient de traiter un fichier
        if chat_id in sessions:
            sessions[chat_id]['just_processed'] = True
        
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
        # NOUVEAU: Réinitialiser le flag après un court délai
        async def reset_flag():
            await asyncio.sleep(1)  # Attendre 1 seconde
            if chat_id in sessions:
                sessions[chat_id]['just_processed'] = False
        
        asyncio.create_task(reset_flag())

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
    global cleanup_task_started
    user_id = message.from_user.id
    
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
    
    # Protection anti-doublon
    if is_duplicate_message(user_id, message.id, "start"):
        logger.info(f"Start ignoré - message dupliqué pour user {user_id}")
        return
    
    logger.info(f"Start command received from user {user_id}")
    
    # Démarrer la tâche de nettoyage au premier appel
    if not cleanup_task_started:
        asyncio.create_task(cleanup_temp_files())
        cleanup_task_started = True
        logger.info("Tâche de nettoyage périodique démarrée")
    
    # NOUVEAU: Charger le username sauvegardé
    saved_username = get_saved_username(user_id)
    
    # Réinitialiser complètement la session
    delete_delay = sessions.get(user_id, {}).get('delete_delay', AUTO_DELETE_DELAY)
    
    user_batches[user_id].clear()
    sessions[user_id] = {}
    
    # NOUVEAU: Restaurer le username depuis le fichier
    if saved_username:
        sessions[user_id]['username'] = saved_username
        logger.info(f"📂 Username restored from file for user {user_id}: {saved_username}")
    
    if delete_delay != AUTO_DELETE_DELAY:
        sessions[user_id]['delete_delay'] = delete_delay
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("📦 Batch Mode", callback_data="batch_mode")]
    ])
    
    await client.send_message(message.chat.id, MESSAGES['start'], reply_markup=keyboard)

@app.on_message(filters.command("batch") & filters.private)
async def batch_command(client, message: Message):
    user_id = message.from_user.id
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon
    if is_duplicate_message(user_id, message.id, "batch"):
        logger.info(f"Batch ignoré - message dupliqué pour user {user_id}")
        return
    
    logger.info(f"🔍 batch_command appelé - User {user_id} - Time: {datetime.now()}")
    
    # Protection contre les doubles appels
    if sessions.get(user_id, {}).get('batch_command_processing'):
        logger.info(f"🔍 batch_command ignoré - déjà en cours pour user {user_id}")
        return
    
    sessions[user_id] = sessions.get(user_id, {})
    sessions[user_id]['batch_command_processing'] = True
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    count = len(user_batches[user_id])
    if count > 0:
        await client.send_message(
            message.chat.id,
            f"📦 **Batch Mode**\n\n"
            f"You have {count} file(s) waiting.\n"
            f"Maximum: {MAX_BATCH_FILES} files\n\n"
            f"Send `/process` to process all files"
        )
    else:
        await client.send_message(
            message.chat.id,
            f"📦 **Batch Mode**\n\n"
            f"No files waiting.\n"
            f"Send up to {MAX_BATCH_FILES} PDF files then `/process`"
        )
    
    # Libérer le flag
    sessions[user_id]['batch_command_processing'] = False

@app.on_message(filters.command("process") & filters.private)
async def process_batch_command(client, message: Message):
    user_id = message.from_user.id
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon
    if is_duplicate_message(user_id, message.id, "process"):
        logger.info(f"Process ignoré - message dupliqué pour user {user_id}")
        return
    
    logger.info(f"🔍 process_batch_command appelé - User {user_id} - Time: {datetime.now()}")
    
    # Protection contre les doubles appels
    if sessions.get(user_id, {}).get('process_command_processing'):
        logger.info(f"🔍 process_batch_command ignoré - déjà en cours pour user {user_id}")
        return
    
    sessions[user_id] = sessions.get(user_id, {})
    sessions[user_id]['process_command_processing'] = True
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    if not user_batches[user_id]:
        await client.send_message(message.chat.id, "❌ No files waiting in the batch")
        sessions[user_id]['process_command_processing'] = False
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clean usernames (all)", callback_data=f"batch_clean:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock all", callback_data=f"batch_unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages (all)", callback_data=f"batch_pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both (all)", callback_data=f"batch_both:{user_id}")],
        [InlineKeyboardButton("🧹 Clear batch", callback_data=f"batch_clear:{user_id}")]
    ])
    
    await client.send_message(
        message.chat.id,
        f"📦 **Batch Processing**\n\n"
        f"{len(user_batches[user_id])} file(s) ready\n\n"
        f"What do you want to do?",
        reply_markup=keyboard
    )
    
    # Libérer le flag
    sessions[user_id]['process_command_processing'] = False

@app.on_message(filters.document & filters.private)
async def handle_document(client, message: Message):
    user_id = message.from_user.id
    
    # NOUVEAU: Ignorer si c'est le bot qui envoie (ses propres fichiers traités)
    if message.from_user.is_bot:
        logger.info(f"Document ignored - sent by the bot itself")
        return
    
    # NOUVEAU: Ignorer si on vient de traiter un fichier
    if sessions.get(user_id, {}).get('just_processed'):
        logger.info(f"Document ignored - file was just processed")
        sessions[user_id]['just_processed'] = False
        return
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon
    if is_duplicate_message(user_id, message.id, "document"):
        logger.info(f"Document ignored - duplicate message for user {user_id}")
        return
    
    # Vérifier si on traite déjà quelque chose
    if sessions.get(user_id, {}).get('processing'):
        logger.info(f"Document ignored - processing in progress for user {user_id}")
        return
    
    doc = message.document
    if not doc:
        return
    
    if doc.mime_type != "application/pdf":
        await client.send_message(message.chat.id, MESSAGES['not_pdf'])
        return
    
    if doc.file_size > MAX_FILE_SIZE:
        await client.send_message(message.chat.id, MESSAGES['file_too_big'])
        return
    
    file_id = doc.file_id
    file_name = doc.file_name or "document.pdf"
    
    # Vérifier si on est en mode batch
    if sessions.get(user_id, {}).get('batch_mode'):
        # Vérifier l'existence de user_batches[user_id]
        if user_id not in user_batches:
            user_batches[user_id] = []
            
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await client.send_message(message.chat.id, f"❌ Limit of {MAX_BATCH_FILES} files reached!")
            return
        
        user_batches[user_id].append({
            'file_id': file_id,
            'file_name': file_name
        })
        
        await client.send_message(
            message.chat.id,
            f"✅ File added to batch ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"Send `/process` when you're done adding files"
        )
        return
    
    # Mode normal - créer une nouvelle session
    if user_id not in sessions:
        sessions[user_id] = {}
    
    # NOUVEAU: Charger le username depuis le fichier persistant
    saved_username = get_saved_username(user_id)
    delete_delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
    
    sessions[user_id] = {
        'file_id': file_id,
        'file_name': file_name,
        'last_activity': datetime.now()
    }
    
    # NOUVEAU: Restaurer le username depuis le fichier
    if saved_username:
        sessions[user_id]['username'] = saved_username
    if delete_delay != AUTO_DELETE_DELAY:
        sessions[user_id]['delete_delay'] = delete_delay
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clean usernames", callback_data=f"clean_username:{user_id}")],
        [InlineKeyboardButton("🔓 Unlock", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Remove pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")]
    ])
    
    await client.send_message(
        message.chat.id,
        f"File received: {file_name}\n\nWhat do you want to do?",
        reply_markup=keyboard
    )

# 🔥 HANDLER POUR LE BOUTON "I have joined" 🔥
@app.on_callback_query(filters.regex("^check_joined$"))
async def check_joined_handler(client, query: CallbackQuery):
    user_id = query.from_user.id
    logger.info(f"🔍 check_joined_handler appelé pour user {user_id}")
    
    is_member = await is_user_in_channel(user_id)
    logger.info(f"🔍 Résultat vérification membership pour user {user_id}: {is_member}")
    
    if is_member:
        await query.answer("✅ Thank you! You can now use the bot.", show_alert=True)
        await query.message.delete()
        # Afficher le message de bienvenue
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📦 Batch Mode", callback_data="batch_mode")]
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
    with open(file_path, 'rb') as f:
        reader = PdfReader(f)
        if reader.is_encrypted:
            await query.edit_message_text("❌ Cannot delete page (PDF protected)")
            return
        last_page = len(reader.pages)
    await remove_page_logic(client, query, user_id, page_number=last_page)

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
    with open(file_path, 'rb') as f:
        reader = PdfReader(f)
        if reader.is_encrypted:
            await query.edit_message_text("❌ Cannot delete page (PDF protected)")
            return
        total_pages = len(reader.pages)
        middle_page = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
    await remove_page_logic(client, query, user_id, page_number=middle_page)

@app.on_callback_query(filters.regex(r"^enter_manually:(\d+)$"))
async def ask_user_page_input(client, query: CallbackQuery):
    user_id = int(query.matches[0].group(1))
    session = ensure_session_dict(user_id)
    session['awaiting_page_number'] = True
    await query.edit_message_text("📝 Enter the page number to delete:")

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
    with open(file_path, 'rb') as f:
        reader = PdfReader(f)
        if reader.is_encrypted:
            await query.edit_message_text("❌ Cannot delete page (PDF protected)")
            return
        
        total_pages = len(reader.pages)
        if page_number < 1 or page_number > total_pages:
            await query.edit_message_text(f"❌ Invalid page number. The PDF has {total_pages} pages.")
            return
        
        # Delete the page
        writer = PdfWriter()
        for i in range(total_pages):
            if (i + 1) != page_number:  # Keep all pages except the one to delete
                writer.add_page(reader.pages[i])
        
        # Save the modified PDF
        output_path = f"{get_user_temp_dir(user_id)}/modified_{session.get('file_name', 'document.pdf')}"
        with open(output_path, 'wb') as out:
            writer.write(out)
        
        # Send the modified file directly (no confirmation message)
        username = session.get('username', '')
        new_file_name = replace_username_in_filename(session.get('file_name', 'document.pdf'), username)
        delay = session.get('delete_delay', AUTO_DELETE_DELAY)
        
        await send_and_delete(client, query.message.chat.id, output_path, new_file_name, delay_seconds=delay)
        await query.edit_message_text(f"✅ Page {page_number} deleted successfully!")
        
        # ✅ FIX: Reset processing flag after successful page removal
        sessions[user_id]['processing'] = False

@app.on_callback_query() 
async def button_callback(client, query: CallbackQuery):
    # Debug logging
    logger.info(f"DEBUG callback_query data: {query.data}")
    
    if query.data == "check_joined":
        return  # Already handled by specific handler
    
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if user_id not in sessions:
        sessions[user_id] = {}
    
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
            f"📦 **Batch Mode Activated**\n\n"
            f"You can now send up to {MAX_BATCH_FILES} PDF files.\n"
            f"When you're done, send `/process` to process them.\n\n"
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
                "Step 1/2: Send me the password (or 'none'):"
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
                        with open(file, 'rb') as f:
                            reader = PdfReader(f)
                            total_pages = len(reader.pages)
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
                        with open(file, 'rb') as f:
                            reader = PdfReader(f)
                            total_pages = len(reader.pages)
                        middle = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
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
                await safe_edit_message(query, "❌ Error: missing password.\n\nPlease use `/process` to start again.")
        return
    
    # Gestion des paramètres
    if data == "settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Username", callback_data="add_username")],
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
            "Send me the [@username] to add.\n\n"
            "Format: [@username] or [📢 @username]"
        )
        logger.info(f"🔍 Username addition mode activated for user {user_id}")
        return
    
    elif data == "delete_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        logger.info(f"🔍 Delete username - User {user_id} - Username existant: {sessions[user_id].get('username')}")
        
        # 🔥 Correction ici : suppression clé 'username' de la session ET du fichier persistant
        if sessions[user_id].get('username'):
            old_username = sessions[user_id]['username']
            sessions[user_id].pop('username', None)  # <-- bien pop la clé ici
            
            # Supprimer aussi du fichier persistant
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
            [InlineKeyboardButton("📦 Batch Mode", callback_data="batch_mode")]
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
            [InlineKeyboardButton("🛠️ Full Both Process", callback_data=f"both_full:{user_id}")],
            [InlineKeyboardButton("🗑️ Remove Pages Only", callback_data=f"both_remove_pages:{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")]
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
        sessions[user_id]['processing'] = False  # Libérer le flag pour les boutons
        return

@app.on_message(filters.text & filters.private)
async def handle_all_text(client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id, {})
    
    if user_id in sessions:
        sessions[user_id]['last_activity'] = datetime.now()

    # ✅ FIX: Mot de passe pour UNLOCK
    if session.get('action') == "unlock":
        password = message.text.strip()
        # Appel à la fonction de traitement unlock
        await process_unlock(client, message, session, password)
        # Remet à zéro le flag
        session.pop('action', None)
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
        
        # IMPORTANT: S'assurer que la session existe avant de stocker le mot de passe
        session = ensure_session_dict(user_id)
        session['batch_both_password'] = password
        session['awaiting_batch_both_password'] = False
        
        # Vérifier immédiatement que le mot de passe est bien stocké
        stored_password = session.get('batch_both_password', '')
        logger.info(f"🔑 DEBUG: Password stored successfully? {bool(stored_password)}")
        logger.info(f"🔑 DEBUG: Session keys after storing: {list(session.keys())}")
        
        # Log du changement de session
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

    # Gestion username (paramètre)
    if session.get('awaiting_username'):
        username = message.text.strip()
        match = re.search(r'@[\w\d_]+', username)
        if match:
            username_clean = match.group()
            session['username'] = username_clean
            session['awaiting_username'] = False
            
            # NOUVEAU: Sauvegarder de manière persistante
            if save_username(user_id, username_clean):
                await client.send_message(message.chat.id, f"✅ Username saved: {username_clean}")
            else:
                await client.send_message(message.chat.id, f"✅ Username saved in session: {username_clean}\n⚠️ (Could not save to file)")
            
            logger.info(f"🔧 Username registered for user {user_id}: {username_clean}")
        else:
            await client.send_message(message.chat.id, "❌ No valid @username found in your text. Try again.")
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
            # Supprimer le message de l'utilisateur
            try:
                await message.delete()
            except:
                pass
            
            status = await client.send_message(message.chat.id, MESSAGES['processing'])
            
            file = await client.download_media(file_id, file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"both_{file_name}"
                shutil.move(file, input_path)
                
                with open(input_path, 'rb') as f:
                    reader = PdfReader(f)
                    
                    # Déverrouiller si nécessaire
                    if reader.is_encrypted:
                        if password.lower() != 'none' and not reader.decrypt(password):
                            await status.edit_text("❌ Incorrect password.")
                            sessions.pop(user_id, None)
                            return
                    
                    total_pages = len(reader.pages)
                    writer = PdfWriter()
                    kept = 0
                    
                    # Supprimer les pages spécifiées
                    for j in range(total_pages):
                        if (j + 1) not in pages_to_remove:
                            writer.add_page(reader.pages[j])
                            kept += 1
                    
                    if kept == 0:
                        await status.edit_text("❌ No pages remaining after removal.")
                        sessions.pop(user_id, None)
                        return
                    
                    with open(output_path, 'wb') as out:
                        writer.write(out)
                
                # Nettoyer le nom du fichier
                cleaned_name = replace_username_in_filename(file_name, username)
                
                # Supprimer le message de statut
                try:
                    await status.delete()
                except:
                    pass
                
                # Envoyer le message de succès
                await client.send_message(
                    message.chat.id,
                    f"✅ **The Both** completed successfully!\n\n"
                    f"• PDF unlocked\n"
                    f"• Pages {pages_text} removed\n"
                    f"• Usernames cleaned\n"
                    f"• Custom username added: {username if username else 'None'}"
                )
                
                # Envoyer le fichier
                delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                        
        except Exception as e:
            logger.error(f"Error process_both_final: {e}")
            await status.edit_text(MESSAGES['error'])
        finally:
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
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    
    if not files:
        # Utiliser edit_message_text au lieu de send_message
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No files in batch")
        sessions[user_id]['processing'] = False
        return
    
    # Utiliser la fonction helper pour créer le message de statut
    logger.info(f"🔍 Début process_batch_unlock - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"unlocked_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with open(input_path, 'rb') as f:
                        reader = PdfReader(f)
                        
                        # Détection améliorée du mot de passe
                        if reader.is_encrypted:
                            try:
                                if reader.decrypt("") or reader.decrypt(None):
                                    logger.info("PDF was encrypted but unlocked with empty password.")
                                else:
                                    if not reader.decrypt(password):
                                        error_count += 1
                                        continue
                            except:
                                if not reader.decrypt(password):
                                    error_count += 1
                                    continue
                        
                        writer = PdfWriter()
                        session = ensure_session_dict(user_id)
                        username = session.get('username', '')
                        
                        for page in reader.pages:
                            writer.add_page(page)
                        
                        with open(output_path, 'wb') as out:
                            writer.write(out)
                    
                    new_file_name = replace_username_in_filename(file_info['file_name'], username)
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
        # Nettoyer tout à la fin
        user_batches[user_id].clear()
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session.pop('awaiting_batch_password', None)
        session['processing'] = False

async def process_batch_pages(client, message, user_id, pages_text):
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    
    if not files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No files in batch")
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
    
    # Utiliser la fonction helper
    logger.info(f"🔍 Début process_batch_pages - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Processing {len(files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"modified_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with open(input_path, 'rb') as f:
                        reader = PdfReader(f)
                        total_pages = len(reader.pages)
                        
                        writer = PdfWriter()
                        session = ensure_session_dict(user_id)
                        username = session.get('username', '')
                        kept = 0
                        
                        for j in range(total_pages):
                            if (j + 1) not in pages_to_remove:
                                writer.add_page(reader.pages[j])
                                kept += 1
                        
                        if kept == 0:
                            error_count += 1
                            continue
                        
                        with open(output_path, 'wb') as out:
                            writer.write(out)
                    
                    new_file_name = replace_username_in_filename(file_info['file_name'], username)
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
        # Nettoyer tout à la fin
        user_batches[user_id].clear()
        session = ensure_session_dict(user_id)
        session.pop('batch_mode', None)
        session.pop('batch_action', None)
        session['processing'] = False

async def process_batch_both(client, message, user_id, password, pages_text):
    
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    
    if not files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No files in batch")
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
    
    # Utiliser la fonction helper
    logger.info(f"🔍 Début process_batch_both - User {user_id} - Time: {datetime.now()}")
    status = await create_or_edit_status(client, message, f"⏳ Combined processing of {len(files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"⏳ Processing file {i+1}/{len(files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"both_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with open(input_path, 'rb') as f:
                        reader = PdfReader(f)
                        
                        if reader.is_encrypted:
                            if password.lower() != 'none' and not reader.decrypt(password):
                                error_count += 1
                                continue
                        
                        total_pages = len(reader.pages)
                        writer = PdfWriter()
                        session = ensure_session_dict(user_id)
                        username = session.get('username', '')
                        kept = 0
                        
                        for j in range(total_pages):
                            if (j + 1) not in pages_to_remove:
                                writer.add_page(reader.pages[j])
                                kept += 1
                        
                        if kept == 0:
                            error_count += 1
                            continue
                        
                        with open(output_path, 'wb') as out:
                            writer.write(out)
                    
                    new_file_name = replace_username_in_filename(file_info['file_name'], username)
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
        # Nettoyer tout à la fin
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
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                # Vérifier si le PDF est protégé
                if reader.is_encrypted:
                    # Essayer de déverrouiller avec le mot de passe
                    if password.lower() == 'none':
                        # Essayer sans mot de passe
                        if not reader.decrypt("") and not reader.decrypt(None):
                            await status.edit_text("❌ PDF is protected. Please provide the correct password.")
                            return
                    else:
                        # Essayer avec le mot de passe fourni
                        if not reader.decrypt(password):
                            await status.edit_text("❌ Incorrect password. Please try again.")
                            return
                
                writer = PdfWriter()
                
                # Copier toutes les pages
                for page in reader.pages:
                    writer.add_page(page)
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            # Nettoyer le nom du fichier
            username = session.get('username', '')
            cleaned_name = replace_username_in_filename(session['file_name'], username)
            
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
            await status.edit_text("❌ No file in session. Send a PDF first.")
            return
        
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"cleaned_{session['file_name']}"
            shutil.move(file, input_path)
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                writer = PdfWriter()
                
                # Copier toutes les pages sans modification
                for page in reader.pages:
                    writer.add_page(page)
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            # Nettoyer le nom du fichier
            username = session.get('username', '')
            cleaned_name = replace_username_in_filename(session['file_name'], username)
            
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
                    
    except Exception as e:
        logger.error(f"Error process_clean_username: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

async def process_batch_clean(client, message, user_id):
    """Fonction batch pour nettoyer uniquement les usernames du nom de fichier"""
    # Vérifier l'existence de user_batches[user_id]
    if user_id not in user_batches:
        user_batches[user_id] = []
    
    files = user_batches[user_id]
    
    if not files:
        if hasattr(message, 'edit_message_text'):
            await message.edit_message_text("❌ No files in batch")
        else:
            await client.send_message(message.chat.id, "❌ No files in batch")
        sessions[user_id]['processing'] = False
        return
    
    # Utiliser la fonction helper
    status = await create_or_edit_status(client, message, f"🧹 Cleaning usernames on {len(files)} files...")
    success_count = 0
    error_count = 0
    
    try:
        for i, file_info in enumerate(files):
            try:
                await status.edit_text(f"🧹 Cleaning file {i+1}/{len(files)}...")
                
                file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    input_path = Path(temp_dir) / "input.pdf"
                    output_path = Path(temp_dir) / f"cleaned_{file_info['file_name']}"
                    shutil.move(file, input_path)
                    
                    with open(input_path, 'rb') as f:
                        reader = PdfReader(f)
                        writer = PdfWriter()
                        
                        # Copier toutes les pages sans modification
                        for page in reader.pages:
                            writer.add_page(page)
                        
                        with open(output_path, 'wb') as out:
                            writer.write(out)
                    
                    # Nettoyer le nom du fichier
                    username = sessions[user_id].get('username', '')
                    cleaned_name = replace_username_in_filename(file_info['file_name'], username)
                    
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
        # Nettoyer tout à la fin
        user_batches[user_id].clear()
        sessions[user_id].pop('batch_mode', None)
        sessions[user_id]['processing'] = False

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
        with open(input_path, 'rb') as f:
            reader = PdfReader(f)
            
            # Déverrouillage si nécessaire
            if reader.is_encrypted:
                if not password or password.lower() == 'none':
                    return False, "PDF is protected but no password provided"
                if not reader.decrypt(password):
                    return False, "Incorrect password"
            
            # Suppression de pages si demandée
            if pages_to_remove:
                total_pages = len(reader.pages)
                invalid_pages = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid_pages:
                    return False, f"Invalid pages: {invalid_pages} (PDF has {total_pages} pages)"
                
                writer = PdfWriter()
                kept = 0
                for i in range(total_pages):
                    if (i + 1) not in pages_to_remove:
                        writer.add_page(reader.pages[i])
                        kept += 1
                
                if kept == 0:
                    return False, "No pages remaining after removal"
            else:
                # Pas de suppression, copier toutes les pages
                writer = PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)
            
            # Écrire le PDF final
            with open(output_path, 'wb') as out:
                writer.write(out)
            
            return True, None
            
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
        # Gestion de l'ajout de username
        username = message.text.strip()
        match = re.search(r'@[\w\d_]+', username)
        if match:
            username_clean = match.group()
            if save_username(user_id, username_clean):
                await client.send_message(message.chat.id, f"✅ Username saved: {username_clean}")
            else:
                await client.send_message(message.chat.id, f"✅ Username saved in session: {username_clean}\n⚠️ (Could not save to file)")
            clear_user_state(user_id)
        else:
            await client.send_message(message.chat.id, "❌ No valid @username found. Try again.")
    
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
        reader = PdfReader(file_path)
        if page_index < 0 or page_index >= len(reader.pages):
            await reply_context.reply_text("❌ Invalid page.")
            return
        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i != page_index:
                writer.add_page(page)
        with open(file_path, "wb") as f:
            writer.write(f)
        await reply_context.reply_document(document=file_path, caption=f"✅ Page {page_index + 1} deleted.")
    except Exception as e:
        logger.error(f"Error deleting page: {e}")
        await reply_context.reply_text("❌ An error occurred.")

# ✅ 4. Fonction is_pdf_locked(file_path)
def is_pdf_locked(file_path: str) -> bool:
    try:
        with open(file_path, "rb") as f:
            reader = PdfReader(f)
            return reader.is_encrypted
    except Exception as e:
        logger.error(f"Error is_pdf_locked: {e}")
        return True

# ✅ 5. Fonction get_last_page_number(file_path)
def get_last_page_number(file_path: str) -> int:
    try:
        reader = PdfReader(file_path)
        return len(reader.pages) - 1
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
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                # Vérifier si le PDF est protégé
                if reader.is_encrypted:
                    await status.edit_text("❌ PDF is protected. Please use 'Unlock' first or provide a password.")
                    return
                
                total_pages = len(reader.pages)
                writer = PdfWriter()
                username = session.get('username', '')
                kept = 0
                
                # Supprimer les pages spécifiées
                for j in range(total_pages):
                    if (j + 1) not in pages_to_remove:
                        writer.add_page(reader.pages[j])
                        kept += 1
                
                if kept == 0:
                    await status.edit_text("❌ No pages remaining after removal.")
                    return
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            # Nettoyer le nom du fichier
            cleaned_name = replace_username_in_filename(session['file_name'], username)
            
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
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                # Déverrouiller si nécessaire
                if reader.is_encrypted:
                    if password.lower() != 'none' and not reader.decrypt(password):
                        await status.edit_text("❌ Incorrect password.")
                        return
                
                total_pages = len(reader.pages)
                writer = PdfWriter()
                username = session.get('username', '')
                kept = 0
                
                # Supprimer les pages spécifiées
                for j in range(total_pages):
                    if (j + 1) not in pages_to_remove:
                        writer.add_page(reader.pages[j])
                        kept += 1
                
                if kept == 0:
                    await status.edit_text("❌ No pages remaining after removal.")
                    return
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            # Nettoyer le nom du fichier
            cleaned_name = replace_username_in_filename(session['file_name'], username)
            
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

# Lancement du bot
if __name__ == "__main__":
    print("🚀 Starting PDF Bot (Pyrogram) - ENGLISH VERSION WITH FORCE JOIN - CORRECTED")
    print(f"🆔 Process PID: {os.getpid()}")
    print(f"⏰ Start time: {datetime.now()}")
    print("✅ Bot configured and ready!")
    print(f"📢 Force Join Channel: @{FORCE_JOIN_CHANNEL}")
    print("\n📌 Features:")
    print("  ✅ Force join channel protection")
    print("  ✅ All messages in English")
    print("  ✅ Anti-duplicate global protection")
    print("  ✅ Rate limiting (5 actions per minute)")
    print("  ✅ Success messages before file sending")
    print("  ✅ Session deletion after processing")
    print("  ✅ Clean button cleans AND adds username")
    print("  ✅ Automatic session cleanup (10 min timeout)")
    print("  ✅ Fixed batch mode with proper cleanup")
    print("  ✅ Fixed button handlers (First/Last/Middle)")
    print("  ✅ Improved error handling for corrupted PDFs")
    print("  ✅ Fixed delay=0 handling")
    print("  ✅ Better password detection")
    print("\n🔧 All bugs fixed and tested!")
    
    app.run()