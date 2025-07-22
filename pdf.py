#!/usr/bin/env python3 
"""
Bot Telegram pour la gestion des PDF
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
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, UsernameNotOccupied

# Import de la configuration
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = os.getenv("ADMIN_IDS")

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

# Protection globale contre les doublons
processed_messages = {}
user_last_command = {}  # {user_id: (command, timestamp)}

def is_duplicate_message(user_id, message_id, command_type="message"):
    """Vérifie si un message a déjà été traité ou si c'est une commande répétée"""
    current_time = datetime.now()
    
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
        return True
    
    try:
        member = await app.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
        return False
    except UserNotParticipant:
        return False
    except ChatAdminRequired:
        logger.error(f"Bot is not admin in channel {FORCE_JOIN_CHANNEL}")
        return True  # On laisse passer pour éviter de bloquer
    except UsernameNotOccupied:
        logger.error(f"Channel {FORCE_JOIN_CHANNEL} does not exist")
        return True
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
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
        reply_markup=keyboard,
        parse_mode="Markdown"
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
        logger.warning(f"Erreur extraction texte: {e}")
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
        logger.error(f"Erreur création/édition statut: {e}")
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
            logger.error(f"Erreur édition message: {e}")
            raise e

async def send_and_delete(client, chat_id, file_path, file_name, caption=None, delay_seconds=AUTO_DELETE_DELAY):
    """Envoie un document et le supprime automatiquement après un délai"""
    try:
        logger.info(f"📤 send_and_delete - Fichier: {file_path} - Existe: {os.path.exists(file_path)}")
        logger.info(f"📤 send_and_delete - Chat: {chat_id} - Nom: {file_name} - Délai: {delay_seconds}")
        
        with open(file_path, 'rb') as f:
            # Ici on ne rajoute pas la phrase de suppression à la caption !
            sent = await client.send_document(
                chat_id, 
                document=f,
                file_name=file_name,
                caption=caption or ""
            )
            logger.info(f"✅ Document envoyé avec succès - ID: {sent.id}")

            # Planifier la suppression
            async def delete_after_delay():
                await asyncio.sleep(delay_seconds)
                try:
                    await sent.delete()
                    logger.info(f"Message supprimé après {delay_seconds}s")
                except Exception as e:
                    logger.error(f"Erreur suppression message: {e}")
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Fichier local supprimé: {file_path}")
                except Exception as e:
                    logger.error(f"Erreur suppression fichier: {e}")

            if delay_seconds > 0:
                asyncio.create_task(delete_after_delay())

    except Exception as e:
        logger.error(f"Erreur send_and_delete: {e}")

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    global cleanup_task_started
    user_id = message.from_user.id
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
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
    
    # Réinitialiser complètement la session
    username = sessions.get(user_id, {}).get('username')
    delete_delay = sessions.get(user_id, {}).get('delete_delay', AUTO_DELETE_DELAY)
    
    user_batches[user_id].clear()
    sessions[user_id] = {}
    
    # Restaurer les paramètres
    if username:
        sessions[user_id]['username'] = username
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
    
    # 🔥 VÉRIFICATION FORCE JOIN 🔥
    if not await is_user_in_channel(user_id):
        await send_force_join_message(client, message)
        return
    
    # Protection anti-doublon
    if is_duplicate_message(user_id, message.id, "document"):
        logger.info(f"Document ignoré - message dupliqué pour user {user_id}")
        return
    
    # Vérifier si on traite déjà quelque chose
    if sessions.get(user_id, {}).get('processing'):
        logger.info(f"Document ignoré - traitement en cours pour user {user_id}")
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
    
    # Conserver username et delete_delay
    username = sessions[user_id].get('username')
    delete_delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
    
    sessions[user_id] = {
        'file_id': file_id,
        'file_name': file_name,
        'last_activity': datetime.now()
    }
    
    # Restaurer les paramètres
    if username:
        sessions[user_id]['username'] = username
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
    
    if await is_user_in_channel(user_id):
        await query.answer("✅ Thank you! You can now use the bot.", show_alert=True)
        await query.message.delete()
        # Afficher le message de bienvenue
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("📦 Batch Mode", callback_data="batch_mode")]
        ])
        await client.send_message(user_id, MESSAGES['start'], reply_markup=keyboard)
    else:
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)

@app.on_callback_query()
async def button_callback(client, query: CallbackQuery):
    if query.data == "check_joined":
        return  # Déjà géré par le handler spécifique
    
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if user_id not in sessions:
        sessions[user_id] = {}
    
    # Vérifier si une action est déjà en cours (sauf pour clean_username)
    if sessions[user_id].get('processing') and not data.startswith("clean_username"):
        await query.answer("⏳ Processing already in progress...", show_alert=True)
        return
    
    # NE PAS marquer processing=True pour clean_username
    if not data.startswith("clean_username") and not data.startswith("cancel"):
        sessions[user_id]['processing'] = True
    
    # Gestion du mode batch
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
    
    # Gestion batch clear
    elif data.startswith("batch_clear:"):
        user_id = int(data.split(":")[1])
        user_batches[user_id].clear()
        await query.edit_message_text("🧹 Batch cleared successfully!")
        sessions[user_id]['processing'] = False
        return
    
    # Gestion des actions batch
    elif data.startswith("batch_"):
        action, user_id = data.split(":")
        user_id = int(user_id)
        
        if action == "batch_clean":
            await process_batch_clean(client, query.message, user_id)
            return
        elif action == "batch_unlock":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'unlock'
            sessions[user_id]['awaiting_batch_password'] = True
            await query.edit_message_text("🔐 Send me the password for all PDFs:")
        
        elif action == "batch_pages":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'pages'
            await query.edit_message_text(
                "📝 Which pages to remove from all files?\n\n"
                "Examples:\n"
                "• 1 → removes page 1\n"
                "• 1,3,5 → removes pages 1, 3 and 5\n"
                "• 1-5 → removes pages 1 to 5"
            )
        
        elif action == "batch_both":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'both'
            sessions[user_id]['awaiting_batch_both_password'] = True
            await query.edit_message_text(
                "🛠️ **The Both - Batch**\n\n"
                "Step 1/2: Send me the password (or 'none'):"
            )
        elif action == "batch_both_first":
            password = sessions[user_id].get('batch_both_password', '')
            if password:
                await process_batch_both(client, query.message, password, "1")
            else:
                await query.edit_message_text("❌ Error: missing password")
        elif action == "batch_both_last":
            password = sessions[user_id].get('batch_both_password', '')
            if password:
                files = user_batches[user_id]
                if files:
                    file = await client.download_media(files[0]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
                    with open(file, 'rb') as f:
                        reader = PdfReader(f)
                        total_pages = len(reader.pages)
                    os.remove(file)
                    await process_batch_both(client, query.message, password, str(total_pages))
                else:
                    await query.edit_message_text("❌ No files in batch")
            else:
                await query.edit_message_text("❌ Error: missing password")
        elif action == "batch_both_middle":
            password = sessions[user_id].get('batch_both_password', '')
            if password:
                files = user_batches[user_id]
                if files:
                    file = await client.download_media(files[0]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
                    with open(file, 'rb') as f:
                        reader = PdfReader(f)
                        total_pages = len(reader.pages)
                    middle = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
                    os.remove(file)
                    await process_batch_both(client, query.message, password, str(middle))
                else:
                    await query.edit_message_text("❌ No files in batch")
            else:
                await query.edit_message_text("❌ Error: missing password")
        elif action == "batch_both_manual":
            sessions[user_id]['awaiting_batch_both_pages'] = True
            await query.edit_message_text(
                "📝 **Manual page entry - Batch**\n\n"
                "Send me the pages to remove:\n"
                "• 1 → removes page 1\n"
                "• 1,3,5 → removes pages 1, 3 and 5\n"
                "• 1-5 → removes pages 1 to 5"
            )
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
        delay = int(data.split("_")[1])
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['delete_delay'] = delay
        
        if delay == 0:
            await query.edit_message_text("✅ Auto-delete disabled")
        else:
            await query.edit_message_text(f"✅ Files will be deleted after {delay//60} minute(s)")
        return
    
    elif data == "add_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        logger.info(f"🔍 Add username - User {user_id} - Session: {sessions[user_id]}")
        logger.info(f"🔍 Username existant: {sessions[user_id].get('username')}")
        
        if sessions[user_id].get('username'):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Delete current", callback_data="delete_username")],
                [InlineKeyboardButton("❌ Cancel", callback_data="settings")]
            ])
            await query.edit_message_text(
                f"⚠️ A username is already registered: {sessions[user_id]['username']}\n\n"
                "You must delete the current username before adding a new one.",
                reply_markup=keyboard
            )
            logger.info(f"🔍 Affichage du message de suppression pour user {user_id}")
            return
        else:
            sessions[user_id]['awaiting_username'] = True
            await query.edit_message_text(
                "Send me the [@username] to add.\n\n"
                "Format: [@username] or [📢 @username]"
            )
            logger.info(f"🔍 Mode ajout username activé pour user {user_id}")
        return
    
    elif data == "delete_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        logger.info(f"🔍 Delete username - User {user_id} - Username existant: {sessions[user_id].get('username')}")
        
        if sessions[user_id].get('username'):
            old_username = sessions[user_id]['username']
            sessions[user_id].pop('username')
            await query.edit_message_text(f"✅ Username deleted: {old_username}")
            logger.info(f"🔍 Username supprimé pour user {user_id}: {old_username}")
        else:
            await query.edit_message_text("ℹ️ No username registered.")
            logger.info(f"🔍 Aucun username à supprimer pour user {user_id}")
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
                InlineKeyboardButton("The first page", callback_data=f"firstpage:{user_id}"),
                InlineKeyboardButton("The last page", callback_data=f"lastpage:{user_id}"),
                InlineKeyboardButton("The middle page", callback_data=f"middlepage:{user_id}")
            ]
        ])
        await query.edit_message_text(
            "Which pages do you want to remove?\n\n"
            "Examples:\n"
            "• 1 → removes page 1\n"
            "• 1,3,5 → removes pages 1, 3 and 5\n"
            "• 1-5 → removes pages 1 to 5",
            reply_markup=page_buttons
        )
    elif action == "both":
        sessions[user_id]['awaiting_both_password'] = True
        await query.edit_message_text(
            "🛠️ **The Both** - Combined action\n\n"
            "This function will:\n"
            "1. Unlock the PDF (if protected)\n"
            "2. Remove selected pages\n"
            "3. Clean @username and hashtags\n"
            "4. Add your custom username\n\n"
            "**Step 1/2:** Send me the PDF password (or 'none' if not protected):"
        )
    elif action == "firstpage":
        await process_pages(client, query.message, sessions[user_id], "1")
    elif action == "lastpage":
        file = await client.download_media(sessions[user_id]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
        with open(file, 'rb') as f:
            reader = PdfReader(f)
            total_pages = len(reader.pages)
        os.remove(file)  # Nettoyer immédiatement
        await process_pages(client, query.message, sessions[user_id], str(total_pages))
    elif action == "middlepage":
        file = await client.download_media(sessions[user_id]['file_id'], file_name=f"{get_user_temp_dir(user_id)}/temp.pdf")
        with open(file, 'rb') as f:
            reader = PdfReader(f)
            total_pages = len(reader.pages)
        middle = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
        os.remove(file)  # Nettoyer immédiatement
        await process_pages(client, query.message, sessions[user_id], str(middle))
    elif action == "both_first":
        sessions[user_id]['both_pages'] = "1"
        await process_both_with_pages(client, query.message, sessions[user_id])
    elif action == "both_last":
        sessions[user_id]['both_pages'] = "last"
        await process_both_with_pages(client, query.message, sessions[user_id])
    elif action == "both_middle":
        sessions[user_id]['both_pages'] = "middle"
        await process_both_with_pages(client, query.message, sessions[user_id])
    elif action == "both_manual":
        sessions[user_id]['awaiting_both_pages'] = True
        await query.edit_message_text(
            "📝 **Manual page entry**\n\n"
            "Send me the pages to remove:\n"
            "• 1 → removes page 1\n"
            "• 1,3,5 → removes pages 1, 3 and 5\n"
            "• 1-5 → removes pages 1 to 5"
        )

@app.on_message(filters.text & filters.private)
async def handle_all_text(client, message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id, {})
    
    if user_id in sessions:
        sessions[user_id]['last_activity'] = datetime.now()

    # Gestion batch
    if session.get('awaiting_batch_password'):
        password = message.text.strip()
        await process_batch_unlock(client, message, password)
        return
    
    if session.get('batch_action') == 'pages' and 'awaiting_batch_password' not in session:
        pages_text = message.text.strip()
        await process_batch_pages(client, message, pages_text)
        return
    
    if session.get('awaiting_batch_both_password'):
        password = message.text.strip()
        session['batch_both_password'] = password
        session['awaiting_batch_both_password'] = False
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("The first", callback_data=f"batch_both_first:{user_id}"),
                InlineKeyboardButton("The last", callback_data=f"batch_both_last:{user_id}"),
                InlineKeyboardButton("The middle", callback_data=f"batch_both_middle:{user_id}")
            ],
            [InlineKeyboardButton("📝 Enter manually", callback_data=f"batch_both_manual:{user_id}")]
        ])
        await client.send_message(
            message.chat.id,
            "✅ Password received!\n\n"
            "**Step 2/2:** Choose pages to remove:",
            reply_markup=keyboard
        )
        return
    
    if session.get('batch_action') == 'both' and session.get('batch_both_password'):
        pages_text = message.text.strip()
        await process_batch_both(client, message, session['batch_both_password'], pages_text)
        return
    
    # Gestion de la saisie manuelle des pages pour batch both
    if session.get('awaiting_batch_both_pages'):
        pages_text = message.text.strip()
        password = session.get('batch_both_password', '')
        if password:
            await process_batch_both(client, message, password, pages_text)
        else:
            await client.send_message(message.chat.id, "❌ Error: missing password")
        return

    # Gestion username (paramètre)
    if session.get('awaiting_username'):
        username = message.text.strip()
        match = re.search(r'@[\w\d_]+', username)
        if match:
            session['username'] = match.group()
            session['awaiting_username'] = False
            await client.send_message(message.chat.id, f"✅ Username saved: {session['username']}")
            logger.info(f"🔧 Username enregistré pour user {user_id}: {session['username']}")
        else:
            await client.send_message(message.chat.id, "❌ No valid @username found in your text. Try again.")
        return

    # Gestion de The Both - saisie manuelle des pages
    if session.get('awaiting_both_pages'):
        pages_text = message.text.strip()
        session['both_pages'] = pages_text
        session['awaiting_both_pages'] = False
        session['awaiting_both_password'] = True
        await client.send_message(
            message.chat.id,
            f"✅ Pages selected: {pages_text}\n\n"
            "**Step 1/2:** Send me the PDF password (or 'none' if not protected):"
        )
        return

    # Gestion de The Both - mot de passe
    if session.get('awaiting_both_password'):
        password = message.text.strip()
        await process_both_password_received(client, message, session, password)
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
        await process_both(client, message, session, message.text)

# Fonctions de traitement batch
async def process_batch_unlock(client, message, password):
    user_id = message.from_user.id
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
                    if reader.is_encrypted:
                        if not reader.decrypt(password):
                            error_count += 1
                            continue
                    
                    writer = PdfWriter()
                    username = sessions[user_id].get('username', '')
                    
                    for page in reader.pages:
                        writer.add_page(page)
                    
                    with open(output_path, 'wb') as out:
                        writer.write(out)
                
                new_file_name = replace_username_in_filename(file_info['file_name'], username)
                delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
                
                await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch unlock fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Processing complete!\n\n"
        f"Successful: {success_count}\n"
        f"Errors: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['awaiting_batch_password'] = False
    # Libérer le flag de traitement
    sessions[user_id]['processing'] = False

async def process_batch_pages(client, message, pages_text):
    user_id = message.from_user.id
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
                    username = sessions[user_id].get('username', '')
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
                delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
                
                await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch pages fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Processing complete!\n\n"
        f"Successful: {success_count}\n"
        f"Errors: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['batch_action'] = None
    # Libérer le flag de traitement
    sessions[user_id]['processing'] = False

async def process_batch_both(client, message, password, pages_text):
    user_id = message.from_user.id
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
                    username = sessions[user_id].get('username', '')
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
                delay = sessions[user_id].get('delete_delay', AUTO_DELETE_DELAY)
                
                await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch both fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Combined processing complete!\n\n"
        f"Successful: {success_count}\n"
        f"Errors: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['batch_action'] = None
    sessions[user_id]['batch_both_password'] = None
    # Libérer le flag de traitement
    sessions[user_id]['processing'] = False

# Modification des fonctions existantes pour utiliser send_and_delete
async def process_unlock(client, message, session, password):
    try:
        await message.delete()
    except:
        pass
    
    user_id = message.from_user.id
    logger.info(f"🔓 process_unlock - User {user_id} - Password length: {len(password)}")
    
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    
    try:
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"unlocked_{session['file_name']}"
            shutil.move(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                logger.info(f"🔓 PDF encrypted: {reader.is_encrypted}")
                
                if not reader.is_encrypted:
                    # PDF non protégé
                    username = session.get('username', '')
                    cleaned_name = replace_username_in_filename(session['file_name'], username)
                    await status.delete()
                    
                    # D'abord envoyer le message
                    await client.send_message(
                        message.chat.id,
                        "ℹ️ This PDF was not protected.\n\n✅ Usernames cleaned in filename!"
                    )
                    
                    # Puis envoyer le fichier
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, input_path, cleaned_name, delay_seconds=delay)
                    
                    # IMPORTANT: Supprimer la session pour éviter les doubles
                    sessions.pop(user_id, None)
                    return
                
                # Tenter de déverrouiller
                decrypt_result = reader.decrypt(password)
                logger.info(f"🔓 Decrypt result: {decrypt_result}")
                
                if not decrypt_result:
                    await status.edit_text("❌ Incorrect password")
                    # Ne pas supprimer la session ici pour permettre de réessayer
                    return
                
                writer = PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            # D'abord envoyer le message de succès
            await client.send_message(message.chat.id, MESSAGES['success_unlock'])
            logger.info(f"✅ Message de succès envoyé pour user {user_id}")
            
            # Préparer le nom du fichier
            username = session.get('username', '')
            new_file_name = replace_username_in_filename(session['file_name'], username)
            logger.info(f"📁 Nom du fichier préparé: {new_file_name}")
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            logger.info(f"📤 Tentative d'envoi du fichier - Délai: {delay}")
            await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
            
            # IMPORTANT: Supprimer la session après succès
            sessions.pop(user_id, None)
                
    except Exception as e:
        logger.error(f"❌ Erreur unlock: {e}")
        logger.exception("Traceback complet:")
        await status.edit_text(MESSAGES['error'])
        # Supprimer la session en cas d'erreur
        sessions.pop(user_id, None)

async def process_pages(client, message, session, pages_text):
    user_id = message.from_user.id
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    pages_to_remove = set()
    
    try:
        for part in pages_text.replace(' ', '').split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                pages_to_remove.update(range(start, end + 1))
            else:
                pages_to_remove.add(int(part))
    except ValueError:
        await status.edit_text("❌ Invalid format. Use: 1,3,5 or 1-5")
        return
        
    try:
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"modified_{session['file_name']}"
            shutil.move(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    await status.edit_text("🔐 This PDF is protected. Send me the password:")
                    session['awaiting_password_for_pages'] = True
                    session['pages_to_remove'] = pages_to_remove
                    return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Invalid pages: {invalid}\n"
                        f"The PDF has {total_pages} pages"
                    )
                    return
                writer = PdfWriter()
                username = session.get('username', '')
                kept = 0
                for i in range(total_pages):
                    if (i + 1) not in pages_to_remove:
                        page = reader.pages[i]
                        cleaned_text = extract_and_clean_pdf_text(page)
                        writer.add_page(page)
                        kept += 1
                        
                if kept == 0:
                    # Aucune page supprimée : republier en nettoyant le nom du fichier
                    username = session.get('username', '')
                    cleaned_name = replace_username_in_filename(session['file_name'], username)
                    await status.delete()
                    caption = "ℹ️ No pages were removed.\n\n✅ Usernames cleaned in filename!"
                    if username:
                        caption += f"\n\nUsername added: {username}"
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, input_path, cleaned_name, caption, delay)
                    return
                    
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            caption = f"{MESSAGES['success_pages']}\n\nRemoved pages: {sorted(pages_to_remove)}"
            username = session.get('username', '')
            if username:
                caption += f"\n\nUsername added: {username}"
            
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
                    
    except Exception as e:
        logger.error(f"Erreur pages: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_both_password_received(client, message, session, password):
    try:
        await message.delete()
    except:
        pass
    
    pages_selected = session.get('both_pages')
    
    if pages_selected:
        await process_both_final(client, message, session, password, pages_selected)
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("The First", callback_data=f"both_first:{message.from_user.id}"),
                InlineKeyboardButton("The Last", callback_data=f"both_last:{message.from_user.id}"),
                InlineKeyboardButton("The Middle", callback_data=f"both_middle:{message.from_user.id}")
            ],
            [InlineKeyboardButton("📝 Enter manually", callback_data=f"both_manual:{message.from_user.id}")]
        ])
        
        session['both_password'] = password
        session['awaiting_both_password'] = False
        session['awaiting_both_pages_selection'] = True
        
        await client.send_message(
            message.chat.id,
            "✅ Password received!\n\n"
            "**Step 2/2:** Choose pages to remove:",
            reply_markup=keyboard
        )

async def process_both_with_pages(client, message, session):
    password = session.get('both_password')
    pages_text = session.get('both_pages')
    
    if not password:
        await message.edit_message_text("❌ Error: missing password")
        return
    
    await process_both_final(client, message, session, password, pages_text)

async def process_both_final(client, message, session, password, pages_text):
    try:
        await message.delete()
    except:
        pass
    user_id = message.from_user.id
    status = await client.send_message(message.chat.id, "🛠️ Combined processing...")
    
    try:
        pages_to_remove = set()
        
        if pages_text in ["last", "middle"]:
            file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
            with open(file, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    if password.lower() == 'none':
                        await status.edit_text("❌ PDF is protected but you said 'none'. Try again with the correct password.")
                        return
                    if not reader.decrypt(password):
                        await status.edit_text("❌ Incorrect password")
                        return
                
                total_pages = len(reader.pages)
                
                if pages_text == "last":
                    pages_to_remove.add(total_pages)
                elif pages_text == "middle":
                    middle = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
                    pages_to_remove.add(middle)
        else:
            try:
                for part in pages_text.replace(' ', '').split(','):
                    if '-' in part:
                        start, end = map(int, part.split('-'))
                        pages_to_remove.update(range(start, end + 1))
                    else:
                        pages_to_remove.add(int(part))
            except ValueError:
                await status.edit_text("❌ Invalid page format. Use: 1,3,5 or 1-5")
                return
        
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"both_{session['file_name']}"
            shutil.move(file, input_path)
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    if password.lower() == 'none':
                        await status.edit_text("❌ PDF is protected but you said 'none'. Try again with the correct password.")
                        return
                    if not reader.decrypt(password):
                        await status.edit_text("❌ Incorrect password")
                        return
                else:
                    # PDF n'est pas protégé : republier en nettoyant le nom du fichier
                    username = session.get('username', '')
                    cleaned_name = replace_username_in_filename(session['file_name'], username)
                    await status.delete()
                    caption = "ℹ️ This PDF was not protected.\n\n✅ Usernames cleaned in filename!"
                    if username:
                        caption += f"\n\nUsername added: {username}"
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, input_path, cleaned_name, caption, delay)
                    return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Invalid pages: {invalid}\n"
                        f"The PDF has {total_pages} pages"
                    )
                    return
                
                writer = PdfWriter()
                username = session.get('username', '')
                kept = 0
                
                for i in range(total_pages):
                    if (i + 1) not in pages_to_remove:
                        page = reader.pages[i]
                        cleaned_text = extract_and_clean_pdf_text(page)
                        writer.add_page(page)
                        kept += 1
                
                if kept == 0:
                    # Aucune page supprimée : republier en nettoyant le nom du fichier
                    username = session.get('username', '')
                    cleaned_name = replace_username_in_filename(session['file_name'], username)
                    await status.delete()
                    caption = "ℹ️ No pages were removed.\n\n✅ Usernames cleaned in filename!"
                    if username:
                        caption += f"\n\nUsername added: {username}"
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, input_path, cleaned_name, caption, delay)
                    return
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            await status.delete()
            
            caption = "✅ **The Both** - Combined processing complete!\n\n"
            username = session.get('username', '')
            if username:
                caption += f"Username added: {username}\n"
            caption += f"Removed pages: {sorted(pages_to_remove)}\n"
            caption += "• PDF unlocked (if protected)\n"
            caption += "• Pages removed\n"
            caption += "• Auto-cleaned @username and hashtags\n"
            caption += "• Custom username added"
            
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
                    
    except Exception as e:
        logger.error(f"Erreur both final: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_pages_with_password(client, message, session, password, pages_to_remove):
    try:
        await message.delete()
    except:
        pass
    user_id = message.from_user.id
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    
    try:
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"modified_{session['file_name']}"
            shutil.move(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if not reader.decrypt(password):
                    await status.edit_text("❌ Incorrect password")
                    return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Invalid pages: {invalid}\n"
                        f"The PDF has {total_pages} pages"
                    )
                    return
                writer = PdfWriter()
                username = session.get('username', '')
                kept = 0
                for i in range(total_pages):
                    if (i + 1) not in pages_to_remove:
                        page = reader.pages[i]
                        cleaned_text = extract_and_clean_pdf_text(page)
                        writer.add_page(page)
                        kept += 1
                if kept == 0:
                    # Aucune page supprimée : republier en nettoyant le nom du fichier
                    username = session.get('username', '')
                    cleaned_name = replace_username_in_filename(session['file_name'], username)
                    await status.delete()
                    caption = "ℹ️ No pages were removed.\n\n✅ Usernames cleaned in filename!"
                    if username:
                        caption += f"\n\nUsername added: {username}"
                    delay = session.get('delete_delay', AUTO_DELETE_DELAY)
                    await send_and_delete(client, message.chat.id, input_path, cleaned_name, caption, delay)
                    return
                with open(output_path, 'wb') as out:
                    writer.write(out)
            await status.delete()
            
            caption = f"{MESSAGES['success_pages']}\n\nRemoved pages: {sorted(pages_to_remove)}"
            username = session.get('username', '')
            if username:
                caption += f"\n\nUsername added: {username}"
            
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
                    
    except Exception as e:
        logger.error(f"Erreur pages with password: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_both(client, message, session, password):
    await process_both_final(client, message, session, password, "")

async def process_clean_username(client, message, session):
    """Fonction dédiée pour nettoyer uniquement les usernames du nom de fichier"""
    user_id = message.from_user.id
    
    # S'assurer que la session existe
    if user_id not in sessions:
        sessions[user_id] = {}
    
    # Marquer qu'on traite seulement un nettoyage
    sessions[user_id]['cleaning_only'] = True
    
    # Éditer le message des boutons au lieu d'envoyer un nouveau
    try:
        await message.edit_message_text("🧹 Cleaning usernames...")
    except:
        status = await client.send_message(message.chat.id, "🧹 Cleaning usernames...")
    
    try:
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
                await message.delete()
            except:
                pass
            
            # Envoyer le message de succès
            await client.send_message(
                message.chat.id,
                "✅ Usernames cleaned in filename!"
            )
            
            # Envoyer le fichier
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            await send_and_delete(client, message.chat.id, output_path, cleaned_name, delay_seconds=delay)
                    
    except Exception as e:
        logger.error(f"Erreur clean_username: {e}")
        await client.send_message(message.chat.id, MESSAGES['error'])
    finally:
        # IMPORTANT: Supprimer complètement la session
        sessions.pop(user_id, None)

async def process_batch_clean(client, message, user_id):
    """Fonction batch pour nettoyer uniquement les usernames du nom de fichier"""
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
            logger.error(f"Erreur batch clean fichier {i}: {e}")
            error_count += 1
    
    await status.edit_message_text(
        f"✅ Cleaning complete!\n\n"
        f"Successful: {success_count}\n"
        f"Errors: {error_count}"
    )
    
    user_batches[user_id].clear()
    # Libérer le flag de traitement
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
                                logger.info(f"Fichier temporaire supprimé: {file_path}")
                    
                    # Supprimer le dossier utilisateur s'il est vide
                    if not any(user_dir.iterdir()):
                        user_dir.rmdir()
                        logger.info(f"Dossier utilisateur vide supprimé: {user_dir}")
            
            # Nettoyer les flags processing bloqués
            for user_id in list(sessions.keys()):
                if sessions[user_id].get('processing'):
                    # Vérifier si le flag est bloqué depuis trop longtemps
                    last_activity = sessions[user_id].get('last_activity')
                    if last_activity and (current_time - last_activity) > timedelta(minutes=5):
                        logger.info(f"Libération du flag processing bloqué pour user {user_id}")
                        sessions[user_id]['processing'] = False
                        
        except Exception as e:
            logger.error(f"Erreur nettoyage fichiers temp: {e}")
        
        await asyncio.sleep(3600)  # Vérifier toutes les heures

# Lancement du bot
if __name__ == "__main__":
    print("🚀 Starting PDF Bot (Pyrogram) - ENGLISH VERSION WITH FORCE JOIN")
    print(f"🆔 Process PID: {os.getpid()}")
    print(f"⏰ Start time: {datetime.now()}")
    print("✅ Bot configured and ready!")
    print(f"📢 Force Join Channel: @{FORCE_JOIN_CHANNEL}")
    print("📌 Features:")
    print("  - Force join channel protection")
    print("  - All messages in English")
    print("  - Anti-duplicate global protection")
    print("  - Success messages before file sending")
    print("  - Session deletion after processing")
    print("  - Clean button cleans AND adds username")
    
    app.run()