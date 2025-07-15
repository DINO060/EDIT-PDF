#!/usr/bin/env python3 
"""
Bot Telegram pour la gestion des PDF
Compatible avec Python 3.13 et python-telegram-bot 21.x
Avec support batch (24 fichiers max) et suppression automatique
"""

import os
import sys
import logging
import tempfile
import re
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 200 * 1024 * 1024))  # 200 MB par défaut
MAX_BATCH_FILES = 24
AUTO_DELETE_DELAY = 300  # 5 minutes

MESSAGES = {
    'start': "🤖 Bot PDF Manager prêt à l'emploi!\n\n📄 **Mode Normal** : Envoie un PDF pour le traiter\n📦 **Mode Batch** : Traite jusqu'à 24 fichiers d'un coup avec `/batch`\n\nEnvoie-moi un PDF pour commencer!",
    'not_pdf': "❌ Ce n'est pas un fichier PDF !",
    'file_too_big': "❌ Fichier trop volumineux !",
    'processing': "⏳ Traitement en cours...",
    'success_unlock': "✅ PDF déverrouillé avec succès !",
    'success_pages': "✅ Pages supprimées avec succès !",
    'error': "❌ Erreur lors du traitement"
}

try:
    from PyPDF2 import PdfReader, PdfWriter, PageObject
except ImportError:
    print("❌ PyPDF2 n'est pas installé!")
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

app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
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

async def send_and_delete(client, chat_id, file_path, file_name, caption=None, delay_seconds=AUTO_DELETE_DELAY):
    """Envoie un document et le supprime automatiquement après un délai"""
    try:
        # Envoyer le message explicatif si caption existe
        if caption:
            await client.send_message(chat_id, caption)
        
        # Envoyer le fichier
        with open(file_path, 'rb') as f:
            if delay_seconds > 0:
                sent = await client.send_document(
                    chat_id, 
                    document=f,
                    file_name=file_name,
                    caption=f"⏰ Ce fichier sera supprimé dans {delay_seconds//60} minutes"
                )
                
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
                
                # Lancer la suppression en arrière-plan
                asyncio.create_task(delete_after_delay())
            else:
                # Pas de suppression automatique
                await client.send_document(
                    chat_id, 
                    document=f,
                    file_name=file_name
                )
        
    except Exception as e:
        logger.error(f"Erreur send_and_delete: {e}")

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    global cleanup_task_started
    user_id = message.from_user.id
    logger.info(f"Start command received from user {user_id}")
    
    # Démarrer la tâche de nettoyage au premier appel
    if not cleanup_task_started:
        asyncio.create_task(cleanup_temp_files())
        cleanup_task_started = True
        logger.info("Tâche de nettoyage périodique démarrée")
    
    # Vider le batch et désactiver le mode batch
    user_batches[user_id].clear()
    if user_id in sessions:
        sessions[user_id]['batch_mode'] = False
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Paramètre", callback_data="settings")],
        [InlineKeyboardButton("📦 Mode Batch", callback_data="batch_mode")]
    ])
    await message.reply_text(MESSAGES['start'], reply_markup=keyboard)

@app.on_message(filters.command("batch") & filters.private)
async def batch_command(client, message: Message):
    user_id = message.from_user.id
    count = len(user_batches[user_id])
    if count > 0:
        await message.reply_text(
            f"📦 **Mode Batch**\n\n"
            f"Tu as {count} fichier(s) en attente.\n"
            f"Maximum: {MAX_BATCH_FILES} fichiers\n\n"
            f"Envoie `/process` pour traiter tous les fichiers"
        )
    else:
        await message.reply_text(
            f"📦 **Mode Batch**\n\n"
            f"Aucun fichier en attente.\n"
            f"Envoie jusqu'à {MAX_BATCH_FILES} fichiers PDF puis `/process`"
        )

@app.on_message(filters.command("process") & filters.private)
async def process_batch_command(client, message: Message):
    user_id = message.from_user.id
    if not user_batches[user_id]:
        await message.reply_text("❌ Aucun fichier en attente dans le batch")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Déverrouiller tous", callback_data=f"batch_unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Supprimer pages (tous)", callback_data=f"batch_pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both (tous)", callback_data=f"batch_both:{user_id}")],
        [InlineKeyboardButton("🧹 Vider le batch", callback_data=f"batch_clear:{user_id}")]
    ])
    
    await message.reply_text(
        f"📦 **Traitement Batch**\n\n"
        f"{len(user_batches[user_id])} fichier(s) prêt(s)\n\n"
        f"Que veux-tu faire?",
        reply_markup=keyboard
    )

@app.on_message(filters.document & filters.private)
async def handle_document(client, message: Message):
    doc = message.document
    if not doc:
        return
    if doc.mime_type != "application/pdf":
        await message.reply_text(MESSAGES['not_pdf'])
        return
    if doc.file_size > MAX_FILE_SIZE:
        await message.reply_text(MESSAGES['file_too_big'])
        return
    
    user_id = message.from_user.id
    file_id = doc.file_id
    file_name = doc.file_name or "document.pdf"
    
    # Vérifier si on est en mode batch
    if sessions.get(user_id, {}).get('batch_mode'):
        if len(user_batches[user_id]) >= MAX_BATCH_FILES:
            await message.reply_text(f"❌ Limite de {MAX_BATCH_FILES} fichiers atteinte!")
            return
        
        user_batches[user_id].append({
            'file_id': file_id,
            'file_name': file_name
        })
        
        await message.reply_text(
            f"✅ Fichier ajouté au batch ({len(user_batches[user_id])}/{MAX_BATCH_FILES})\n\n"
            f"Envoie `/process` quand tu as fini d'ajouter des fichiers"
        )
        return
    
    # Mode normal (fichier unique)
    sessions[user_id] = sessions.get(user_id, {})
    sessions[user_id].update({
        'file_id': file_id,
        'file_name': file_name,
        'last_activity': datetime.now()
    })
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔓 Déverrouiller", callback_data=f"unlock:{user_id}")],
        [InlineKeyboardButton("🗑️ Supprimer des pages", callback_data=f"pages:{user_id}")],
        [InlineKeyboardButton("🛠️ The Both", callback_data=f"both:{user_id}")],
        [InlineKeyboardButton("❌ Annuler", callback_data=f"cancel:{user_id}")]
    ])
    
    await message.reply_text(
        f"Fichier reçu: {file_name}\n\nQue veux-tu faire?",
        reply_markup=keyboard
    )

@app.on_callback_query()
async def button_callback(client, query: CallbackQuery):
    data = query.data
    
    # Gestion du mode batch
    if data == "batch_mode":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['batch_mode'] = True
        
        await query.edit_message_text(
            f"📦 **Mode Batch Activé**\n\n"
            f"Tu peux maintenant envoyer jusqu'à {MAX_BATCH_FILES} fichiers PDF.\n"
            f"Quand tu as fini, envoie `/process` pour les traiter.\n\n"
            f"Pour désactiver le mode batch, envoie `/start`"
        )
        return
    
    # Gestion batch clear
    elif data.startswith("batch_clear:"):
        user_id = int(data.split(":")[1])
        user_batches[user_id].clear()
        await query.edit_message_text("🧹 Batch vidé avec succès!")
        return
    
    # Gestion des actions batch
    elif data.startswith("batch_"):
        action, user_id = data.split(":")
        user_id = int(user_id)
        
        if action == "batch_unlock":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'unlock'
            sessions[user_id]['awaiting_batch_password'] = True
            await query.edit_message_text("🔐 Envoie-moi le mot de passe pour tous les PDFs:")
        
        elif action == "batch_pages":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'pages'
            await query.edit_message_text(
                "📝 Quelles pages supprimer sur tous les fichiers?\n\n"
                "Exemples:\n"
                "• 1 → supprime page 1\n"
                "• 1,3,5 → supprime pages 1, 3 et 5\n"
                "• 1-5 → supprime pages 1 à 5"
            )
        
        elif action == "batch_both":
            sessions[user_id] = sessions.get(user_id, {})
            sessions[user_id]['batch_action'] = 'both'
            sessions[user_id]['awaiting_batch_both_password'] = True
            await query.edit_message_text(
                "🛠️ **The Both - Batch**\n\n"
                "Étape 1/2: Envoie-moi le mot de passe (ou 'none'):"
            )
        return
    
    # Gestion des paramètres
    if data == "settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Username", callback_data="add_username")],
            [InlineKeyboardButton("⏰ Délai suppression", callback_data="set_delete_delay")],
            [InlineKeyboardButton("🔙 Retour", callback_data="back_to_start")]
        ])
        await query.edit_message_text(
            "⚙️ **Paramètres**\n\n"
            "Configure ton bot selon tes besoins.",
            reply_markup=keyboard
        )
        return
    
    elif data == "set_delete_delay":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 minute", callback_data="delay_60")],
            [InlineKeyboardButton("5 minutes", callback_data="delay_300")],
            [InlineKeyboardButton("10 minutes", callback_data="delay_600")],
            [InlineKeyboardButton("30 minutes", callback_data="delay_1800")],
            [InlineKeyboardButton("Jamais", callback_data="delay_0")],
            [InlineKeyboardButton("🔙 Retour", callback_data="settings")]
        ])
        await query.edit_message_text(
            "⏰ **Délai de suppression automatique**\n\n"
            "Après combien de temps supprimer les fichiers?",
            reply_markup=keyboard
        )
        return
    
    elif data.startswith("delay_"):
        delay = int(data.split("_")[1])
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['delete_delay'] = delay
        
        if delay == 0:
            await query.edit_message_text("✅ Suppression automatique désactivée")
        else:
            await query.edit_message_text(f"✅ Les fichiers seront supprimés après {delay//60} minute(s)")
        return
    
    elif data == "add_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        logger.info(f"🔍 Add username - User {user_id} - Session: {sessions[user_id]}")
        logger.info(f"🔍 Username existant: {sessions[user_id].get('username')}")
        
        if sessions[user_id].get('username'):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Supprimer l'ancien", callback_data="delete_username")],
                [InlineKeyboardButton("❌ Annuler", callback_data="settings")]
            ])
            await query.edit_message_text(
                f"⚠️ Un username est déjà enregistré : {sessions[user_id]['username']}\n\n"
                "Tu dois d'abord supprimer l'ancien username avant d'en ajouter un nouveau.",
                reply_markup=keyboard
            )
            logger.info(f"🔍 Affichage du message de suppression pour user {user_id}")
            return
        else:
            sessions[user_id]['awaiting_username'] = True
            await query.edit_message_text(
                "Envoie-moi maintenant le [@username] à ajouter.\n\n"
                "Format: [@username] ou [📢 @username]"
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
            await query.edit_message_text(f"✅ Username supprimé : {old_username}")
            logger.info(f"🔍 Username supprimé pour user {user_id}: {old_username}")
        else:
            await query.edit_message_text("ℹ️ Aucun username enregistré.")
            logger.info(f"🔍 Aucun username à supprimer pour user {user_id}")
        return
    
    elif data == "back_to_start":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Paramètre", callback_data="settings")],
            [InlineKeyboardButton("📦 Mode Batch", callback_data="batch_mode")]
        ])
        await query.edit_message_text(MESSAGES['start'], reply_markup=keyboard)
        return
    
    # Gestion des actions PDF (suite du code original)
    if ":" not in data:
        return
    
    action, user_id = data.split(":")
    user_id = int(user_id)
    
    if action == "cancel":
        sessions.pop(user_id, None)
        await query.edit_message_text("❌ Opération annulée")
        return
    
    if user_id not in sessions:
        await query.edit_message_text("❌ Session expirée. Renvoie le PDF.")
        return
    
    sessions[user_id]['action'] = action
    
    if action == "unlock":
        await query.edit_message_text("🔐 Envoie-moi le mot de passe du PDF:")
    elif action == "pages":
        page_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("The first page", callback_data=f"firstpage:{user_id}"),
                InlineKeyboardButton("The last page", callback_data=f"lastpage:{user_id}"),
                InlineKeyboardButton("The middle page", callback_data=f"middlepage:{user_id}")
            ]
        ])
        await query.edit_message_text(
            "Quelles pages veux-tu supprimer?\n\n"
            "Exemples:\n"
            "• 1 → supprime page 1\n"
            "• 1,3,5 → supprime pages 1, 3 et 5\n"
            "• 1-5 → supprime pages 1 à 5",
            reply_markup=page_buttons
        )
    elif action == "both":
        sessions[user_id]['awaiting_both_password'] = True
        await query.edit_message_text(
            "🛠️ **The Both** - Action combinée\n\n"
            "Cette fonction va:\n"
            "1. Déverrouiller le PDF (si protégé)\n"
            "2. Supprimer les pages sélectionnées\n"
            "3. Nettoyer les @username et hashtags\n"
            "4. Ajouter ton username personnalisé\n\n"
            "**Étape 1/2 :** Envoie-moi le mot de passe du PDF (ou 'none' si non protégé):"
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
            "📝 **Saisie manuelle des pages**\n\n"
            "Envoie-moi les pages à supprimer au format :\n"
            "• 1 → supprime page 1\n"
            "• 1,3,5 → supprime pages 1, 3 et 5\n"
            "• 1-5 → supprime pages 1 à 5"
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
        await message.reply_text(
            "✅ Mot de passe reçu!\n\n"
            "**Étape 2/2:** Quelles pages supprimer? (ex: 1,3,5 ou 1-5)"
        )
        return
    
    if session.get('batch_action') == 'both' and session.get('batch_both_password'):
        pages_text = message.text.strip()
        await process_batch_both(client, message, session['batch_both_password'], pages_text)
        return

    # Gestion username (paramètre)
    if session.get('awaiting_username'):
        username = message.text.strip()
        match = re.search(r'@[\w\d_]+', username)
        if match:
            session['username'] = match.group()
            session['awaiting_username'] = False
            await message.reply_text(f"✅ Username sauvegardé : {session['username']}")
            logger.info(f"🔧 Username enregistré pour user {user_id}: {session['username']}")
        else:
            await message.reply_text("❌ Aucun @username valide trouvé dans ton texte. Réessaie.")
        return

    # Gestion de The Both - saisie manuelle des pages
    if session.get('awaiting_both_pages'):
        pages_text = message.text.strip()
        session['both_pages'] = pages_text
        session['awaiting_both_pages'] = False
        session['awaiting_both_password'] = True
        await message.reply_text(
            f"✅ Pages sélectionnées : {pages_text}\n\n"
            "**Étape 1/2 :** Envoie-moi le mot de passe du PDF (ou 'none' si non protégé):"
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
        await message.reply_text("❌ Aucun fichier dans le batch")
        return
    
    status = await message.reply_text(f"⏳ Traitement de {len(files)} fichiers...")
    success_count = 0
    error_count = 0
    
    for i, file_info in enumerate(files):
        try:
            await status.edit_text(f"⏳ Traitement fichier {i+1}/{len(files)}...")
            
            file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"unlocked_{file_info['file_name']}"
                os.rename(file, input_path)
                
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
                
                if delay > 0:
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                else:
                    with open(output_path, 'rb') as f:
                        await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch unlock fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Traitement terminé!\n\n"
        f"Réussis: {success_count}\n"
        f"Erreurs: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['awaiting_batch_password'] = False

async def process_batch_pages(client, message, pages_text):
    user_id = message.from_user.id
    files = user_batches[user_id]
    
    if not files:
        await message.reply_text("❌ Aucun fichier dans le batch")
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
        await message.reply_text("❌ Format invalide. Utilise: 1,3,5 ou 1-5")
        return
    
    status = await message.reply_text(f"⏳ Traitement de {len(files)} fichiers...")
    success_count = 0
    error_count = 0
    
    for i, file_info in enumerate(files):
        try:
            await status.edit_text(f"⏳ Traitement fichier {i+1}/{len(files)}...")
            
            file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"modified_{file_info['file_name']}"
                os.rename(file, input_path)
                
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
                
                if delay > 0:
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                else:
                    with open(output_path, 'rb') as f:
                        await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch pages fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Traitement terminé!\n\n"
        f"Réussis: {success_count}\n"
        f"Erreurs: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['batch_action'] = None

async def process_batch_both(client, message, password, pages_text):
    user_id = message.from_user.id
    files = user_batches[user_id]
    
    if not files:
        await message.reply_text("❌ Aucun fichier dans le batch")
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
        await message.reply_text("❌ Format invalide. Utilise: 1,3,5 ou 1-5")
        return
    
    status = await message.reply_text(f"⏳ Traitement combiné de {len(files)} fichiers...")
    success_count = 0
    error_count = 0
    
    for i, file_info in enumerate(files):
        try:
            await status.edit_text(f"⏳ Traitement fichier {i+1}/{len(files)}...")
            
            file = await client.download_media(file_info['file_id'], file_name=f"{get_user_temp_dir(user_id)}/batch_{i}.pdf")
            
            with tempfile.TemporaryDirectory() as temp_dir:
                input_path = Path(temp_dir) / "input.pdf"
                output_path = Path(temp_dir) / f"both_{file_info['file_name']}"
                os.rename(file, input_path)
                
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
                
                if delay > 0:
                    await send_and_delete(client, message.chat.id, output_path, new_file_name, delay_seconds=delay)
                else:
                    with open(output_path, 'rb') as f:
                        await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                
                success_count += 1
                
        except Exception as e:
            logger.error(f"Erreur batch both fichier {i}: {e}")
            error_count += 1
    
    await status.edit_text(
        f"✅ Traitement combiné terminé!\n\n"
        f"Réussis: {success_count}\n"
        f"Erreurs: {error_count}"
    )
    
    user_batches[user_id].clear()
    sessions[user_id]['batch_action'] = None
    sessions[user_id]['batch_both_password'] = None

# Modification des fonctions existantes pour utiliser send_and_delete
async def process_unlock(client, message, session, password):
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
            output_path = Path(temp_dir) / f"unlocked_{session['file_name']}"
            os.rename(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                if not reader.is_encrypted:
                    await status.edit_text("ℹ️ Ce PDF n'est pas protégé")
                    return
                if not reader.decrypt(password):
                    await status.edit_text("❌ Mot de passe incorrect")
                    return
                writer = PdfWriter()
                username = session.get('username', '')
                
                for page in reader.pages:
                    cleaned_text = extract_and_clean_pdf_text(page)
                    writer.add_page(page)
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            caption = MESSAGES['success_unlock']
            if username:
                caption += f"\n\nUsername ajouté: {username}"
            
            username = session.get('username')
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            if delay > 0:
                await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
            else:
                await client.send_message(message.chat.id, caption)
                with open(output_path, 'rb') as f:
                    await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                
    except Exception as e:
        logger.error(f"Erreur unlock: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_pages(client, message, session, pages_text):
    user_id = message.from_user.id
    status = await message.reply_text(MESSAGES['processing'])
    pages_to_remove = set()
    
    try:
        for part in pages_text.replace(' ', '').split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                pages_to_remove.update(range(start, end + 1))
            else:
                pages_to_remove.add(int(part))
    except ValueError:
        await status.edit_text("❌ Format invalide. Utilise: 1,3,5 ou 1-5")
        return
        
    try:
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"modified_{session['file_name']}"
            os.rename(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    await status.edit_text("🔐 Ce PDF est protégé. Envoie-moi le mot de passe:")
                    session['awaiting_password_for_pages'] = True
                    session['pages_to_remove'] = pages_to_remove
                    return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Pages invalides: {invalid}\n"
                        f"Le PDF a {total_pages} pages"
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
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                    
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            caption = f"{MESSAGES['success_pages']}\n\nPages supprimées: {sorted(pages_to_remove)}"
            if username:
                caption += f"\n\nUsername ajouté: {username}"
            
            username = session.get('username')
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            if delay > 0:
                await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
            else:
                await client.send_message(message.chat.id, caption)
                with open(output_path, 'rb') as f:
                    await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                    
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
            [InlineKeyboardButton("📝 Saisir manuellement", callback_data=f"both_manual:{message.from_user.id}")]
        ])
        
        session['both_password'] = password
        session['awaiting_both_password'] = False
        session['awaiting_both_pages_selection'] = True
        
        await message.reply_text(
            "✅ Mot de passe reçu!\n\n"
            "**Étape 2/2 :** Choisis les pages à supprimer:",
            reply_markup=keyboard
        )

async def process_both_with_pages(client, message, session):
    password = session.get('both_password')
    pages_text = session.get('both_pages')
    
    if not password:
        await message.edit_message_text("❌ Erreur: mot de passe manquant")
        return
    
    await process_both_final(client, message, session, password, pages_text)

async def process_both_final(client, message, session, password, pages_text):
    try:
        await message.delete()
    except:
        pass
    user_id = message.from_user.id
    status = await client.send_message(message.chat.id, "🛠️ Traitement combiné en cours...")
    
    try:
        pages_to_remove = set()
        
        if pages_text in ["last", "middle"]:
            file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
            with open(file, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    if password.lower() == 'none':
                        await status.edit_text("❌ Le PDF est protégé mais tu as dit 'none'. Réessaie avec le bon mot de passe.")
                        return
                    if not reader.decrypt(password):
                        await status.edit_text("❌ Mot de passe incorrect")
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
                await status.edit_text("❌ Format invalide pour les pages. Utilise: 1,3,5 ou 1-5")
                return
        
        file = await client.download_media(session['file_id'], file_name=f"{get_user_temp_dir(user_id)}/input.pdf")
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.pdf"
            output_path = Path(temp_dir) / f"both_{session['file_name']}"
            os.rename(file, input_path)
            
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if reader.is_encrypted:
                    if password.lower() == 'none':
                        await status.edit_text("❌ Le PDF est protégé mais tu as dit 'none'. Réessaie avec le bon mot de passe.")
                        return
                    if not reader.decrypt(password):
                        await status.edit_text("❌ Mot de passe incorrect")
                        return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Pages invalides: {invalid}\n"
                        f"Le PDF a {total_pages} pages"
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
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            await status.delete()
            
            caption = "✅ **The Both** - Traitement combiné terminé!\n\n"
            if username:
                caption += f"Username ajouté: {username}\n"
            caption += f"Pages supprimées: {sorted(pages_to_remove)}\n"
            caption += "• PDF déverrouillé (si protégé)\n"
            caption += "• Pages supprimées\n"
            caption += "• Nettoyage automatique des @username et hashtags\n"
            caption += "• Username personnalisé ajouté"
            
            username = session.get('username')
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            if delay > 0:
                await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
            else:
                await client.send_message(message.chat.id, caption)
                with open(output_path, 'rb') as f:
                    await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                    
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
            os.rename(file, input_path)
            with open(input_path, 'rb') as f:
                reader = PdfReader(f)
                
                if not reader.decrypt(password):
                    await status.edit_text("❌ Mot de passe incorrect")
                    return
                
                total_pages = len(reader.pages)
                invalid = [p for p in pages_to_remove if p < 1 or p > total_pages]
                if invalid:
                    await status.edit_text(
                        f"❌ Pages invalides: {invalid}\n"
                        f"Le PDF a {total_pages} pages"
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
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                with open(output_path, 'wb') as out:
                    writer.write(out)
            await status.delete()
            
            caption = f"{MESSAGES['success_pages']}\n\nPages supprimées: {sorted(pages_to_remove)}"
            if username:
                caption += f"\n\nUsername ajouté: {username}"
            
            username = session.get('username')
            new_file_name = replace_username_in_filename(session['file_name'], username)
            
            # Utiliser send_and_delete avec le délai personnalisé
            delay = session.get('delete_delay', AUTO_DELETE_DELAY)
            if delay > 0:
                await send_and_delete(client, message.chat.id, output_path, new_file_name, caption, delay)
            else:
                await client.send_message(message.chat.id, caption)
                with open(output_path, 'rb') as f:
                    await client.send_document(message.chat.id, document=f, file_name=new_file_name)
                    
    except Exception as e:
        logger.error(f"Erreur pages with password: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_both(client, message, session, password):
    await process_both_final(client, message, session, password, "")

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
        except Exception as e:
            logger.error(f"Erreur nettoyage fichiers temp: {e}")
        
        await asyncio.sleep(3600)  # Vérifier toutes les heures

# Lancement du bot
if __name__ == "__main__":
    print("🚀 Démarrage du bot PDF (Pyrogram) avec Batch et Suppression Auto...")
    print("✅ Bot configuré et prêt!")
    print("👉 Envoie /start au bot pour commencer")
    print("📦 Envoie /batch pour activer le mode batch")
    print("🗑️ Les fichiers sont supprimés automatiquement après 5 minutes par défaut")
    print("🧹 Nettoyage automatique des fichiers > 1 heure")
    
    app.run()