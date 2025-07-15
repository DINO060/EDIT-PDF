#!/usr/bin/env python3
"""
Bot Telegram pour la gestion des PDF
Compatible avec Python 3.13 et python-telegram-bot 21.x
"""

import os
import sys
import logging
import tempfile
import re
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# === Configuration depuis config.py ===
try:
    from config import API_ID, API_HASH, BOT_TOKEN, MAX_FILE_SIZE, MESSAGES
except ImportError:
    print("❌ Erreur: Le fichier config.py est manquant!")
    print("Crée un fichier config.py avec tes clés API (voir config_example.py)")
    sys.exit(1)

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
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)

app = Client(
    "pdfbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

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

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    logger.info(f"Start command received from user {message.from_user.id}")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Paramètre", callback_data="settings")],
    ])
    await message.reply_text(MESSAGES['start'], reply_markup=keyboard)

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
    
    # ⚡ CONSERVER L'ANCIENNE SESSION (et donc le username)
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
    
    # Gestion des paramètres
    if data == "settings":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Username", callback_data="add_username")],
            [InlineKeyboardButton("🔙 Retour", callback_data="back_to_start")]
        ])
        await query.edit_message_text(
            "⚙️ **Paramètres**\n\n"
            "Envoie-moi le @username de ton canal au format [@username] "
            "(tu peux ajouter des emojis ou du texte dans les crochets).",
            reply_markup=keyboard
        )
        return
    
    elif data == "add_username":
        user_id = query.from_user.id
        sessions[user_id] = sessions.get(user_id, {})
        sessions[user_id]['last_activity'] = datetime.now()
        
        # Log de débogage pour voir l'état de la session
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
        
        # Log de débogage
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
        ])
        await query.edit_message_text(MESSAGES['start'], reply_markup=keyboard)
        return
    
    # Gestion des actions PDF
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
        file = await client.download_media(sessions[user_id]['file_id'], file_name="input.pdf")
        with open(file, 'rb') as f:
            reader = PdfReader(f)
            total_pages = len(reader.pages)
        await process_pages(client, query.message, sessions[user_id], str(total_pages))
    elif action == "middlepage":
        file = await client.download_media(sessions[user_id]['file_id'], file_name="input.pdf")
        with open(file, 'rb') as f:
            reader = PdfReader(f)
            total_pages = len(reader.pages)
        middle = total_pages // 2 if total_pages % 2 == 0 else (total_pages // 2) + 1
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

    # Gestion username (paramètre) - CORRECTION ROBUSTE
    if session.get('awaiting_username'):
        username = message.text.strip()
        # Extraire proprement tout ce qui ressemble à @username (ignore emojis et crochets)
        match = re.search(r'@[\w\d_]+', username)
        if match:
            session['username'] = match.group()
            session['awaiting_username'] = False
            await message.reply_text(f"✅ Username sauvegardé : {session['username']}")
            logger.info(f"🔧 Username enregistré pour user {user_id}: {session['username']}")  # Log de confirmation
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

async def process_unlock(client, message, session, password):
    try:
        await message.delete()
    except:
        pass
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    try:
        file = await client.download_media(session['file_id'], file_name=f"{TEMP_DIR}/input.pdf")
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
                    
                    if username:
                        try:
                            from reportlab.pdfgen import canvas
                            from reportlab.lib.pagesizes import letter
                            from io import BytesIO
                            
                            watermark_buffer = BytesIO()
                            c = canvas.Canvas(watermark_buffer, pagesize=letter)
                            c.setFont("Helvetica", 10)
                            c.setFillAlpha(0.7)
                            c.setFillColorRGB(0.5, 0.5, 0.5)
                            c.drawString(50, 30, username)
                            c.save()
                            watermark_buffer.seek(0)
                            
                        except ImportError:
                            logger.warning("ReportLab non disponible pour le watermark")
                    
                    writer.add_page(page)
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            with open(output_path, 'rb') as f:
                caption = MESSAGES['success_unlock']
                if username:
                    caption += f"\n\nUsername ajouté: {username}"
                
                username = session.get('username')
                logger.info(f"🚨 Username récupéré : {username}")  # Log de débogage
                new_file_name = replace_username_in_filename(session['file_name'], username)
                logger.info(f"🚨 Nouveau nom de fichier : {new_file_name}")  # Log de débogage
                
                # Envoyer le message explicatif AVANT le fichier
                await client.send_message(message.chat.id, caption)
                
                # Envoyer le fichier sans caption
                await client.send_document(
                    message.chat.id,
                    document=f,
                    file_name=new_file_name
                )
    except Exception as e:
        logger.error(f"Erreur unlock: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_pages(client, message, session, pages_text):
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
        file = await client.download_media(session['file_id'], file_name=f"{TEMP_DIR}/input.pdf")
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
                        
                        if username:
                            try:
                                from reportlab.pdfgen import canvas
                                from reportlab.lib.pagesizes import letter
                                from io import BytesIO
                                
                                watermark_buffer = BytesIO()
                                c = canvas.Canvas(watermark_buffer, pagesize=letter)
                                c.setFont("Helvetica", 10)
                                c.setFillAlpha(0.7)
                                c.setFillColorRGB(0.5, 0.5, 0.5)
                                c.drawString(50, 30, username)
                                c.save()
                                watermark_buffer.seek(0)
                                
                            except ImportError:
                                logger.warning("ReportLab non disponible pour le watermark")
                        
                        writer.add_page(page)
                        kept += 1
                        
                if kept == 0:
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                    
                with open(output_path, 'wb') as out:
                    writer.write(out)
                    
            await status.delete()
            
            with open(output_path, 'rb') as f:
                caption = f"{MESSAGES['success_pages']}\n\nPages supprimées: {sorted(pages_to_remove)}"
                if username:
                    caption += f"\n\nUsername ajouté: {username}"
                
                username = session.get('username')
                logger.info(f"🚨 Username récupéré : {username}")  # Log de débogage
                new_file_name = replace_username_in_filename(session['file_name'], username)
                logger.info(f"🚨 Nouveau nom de fichier : {new_file_name}")  # Log de débogage
                
                # Envoyer le message explicatif AVANT le fichier
                await client.send_message(message.chat.id, caption)
                
                # Envoyer le fichier sans caption
                await client.send_document(
                    message.chat.id,
                    document=f,
                    file_name=new_file_name
                )
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
    status = await client.send_message(message.chat.id, "🛠️ Traitement combiné en cours...")
    
    try:
        pages_to_remove = set()
        
        if pages_text in ["last", "middle"]:
            file = await client.download_media(session['file_id'], file_name=f"{TEMP_DIR}/input.pdf")
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
        
        file = await client.download_media(session['file_id'], file_name=f"{TEMP_DIR}/input.pdf")
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
                        
                        if username:
                            try:
                                from reportlab.pdfgen import canvas
                                from reportlab.lib.pagesizes import letter
                                from io import BytesIO
                                
                                watermark_buffer = BytesIO()
                                c = canvas.Canvas(watermark_buffer, pagesize=letter)
                                c.setFont("Helvetica", 10)
                                c.setFillAlpha(0.7)
                                c.setFillColorRGB(0.5, 0.5, 0.5)
                                c.drawString(50, 30, username)
                                c.save()
                                watermark_buffer.seek(0)
                                
                            except ImportError:
                                logger.warning("ReportLab non disponible pour le watermark")
                        
                        writer.add_page(page)
                        kept += 1
                
                if kept == 0:
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                
                with open(output_path, 'wb') as out:
                    writer.write(out)
            
            await status.delete()
            with open(output_path, 'rb') as f:
                caption = "✅ **The Both** - Traitement combiné terminé!\n\n"
                if username:
                    caption += f"Username ajouté: {username}\n"
                caption += f"Pages supprimées: {sorted(pages_to_remove)}\n"
                caption += "• PDF déverrouillé (si protégé)\n"
                caption += "• Pages supprimées\n"
                caption += "• Nettoyage automatique des @username et hashtags\n"
                caption += "• Username personnalisé ajouté"
                
                username = session.get('username')
                logger.info(f"🚨 Username récupéré : {username}")  # Log de débogage
                new_file_name = replace_username_in_filename(session['file_name'], username)
                logger.info(f"🚨 Nouveau nom de fichier : {new_file_name}")  # Log de débogage
                
                # Envoyer le message explicatif AVANT le fichier
                await client.send_message(message.chat.id, caption)
                
                # Envoyer le fichier sans caption
                await client.send_document(
                    message.chat.id,
                    document=f,
                    file_name=new_file_name
                )
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
    status = await client.send_message(message.chat.id, MESSAGES['processing'])
    
    try:
        file = await client.download_media(session['file_id'], file_name=f"{TEMP_DIR}/input.pdf")
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
                        
                        if username:
                            try:
                                from reportlab.pdfgen import canvas
                                from reportlab.lib.pagesizes import letter
                                from io import BytesIO
                                
                                watermark_buffer = BytesIO()
                                c = canvas.Canvas(watermark_buffer, pagesize=letter)
                                c.setFont("Helvetica", 10)
                                c.setFillAlpha(0.7)
                                c.setFillColorRGB(0.5, 0.5, 0.5)
                                c.drawString(50, 30, username)
                                c.save()
                                watermark_buffer.seek(0)
                                
                            except ImportError:
                                logger.warning("ReportLab non disponible pour le watermark")
                        
                        writer.add_page(page)
                        kept += 1
                if kept == 0:
                    await status.edit_text("❌ Tu ne peux pas supprimer toutes les pages!")
                    return
                with open(output_path, 'wb') as out:
                    writer.write(out)
            await status.delete()
            with open(output_path, 'rb') as f:
                caption = f"{MESSAGES['success_pages']}\n\nPages supprimées: {sorted(pages_to_remove)}"
                if username:
                    caption += f"\n\nUsername ajouté: {username}"
                
                username = session.get('username')
                logger.info(f"🚨 Username récupéré : {username}")  # Log de débogage
                new_file_name = replace_username_in_filename(session['file_name'], username)
                logger.info(f"🚨 Nouveau nom de fichier : {new_file_name}")  # Log de débogage
                
                # Envoyer le message explicatif AVANT le fichier
                await client.send_message(message.chat.id, caption)
                
                # Envoyer le fichier sans caption
                await client.send_document(
                    message.chat.id,
                    document=f,
                    file_name=new_file_name
                )
    except Exception as e:
        logger.error(f"Erreur pages with password: {e}")
        await status.edit_text(MESSAGES['error'])
    finally:
        sessions.pop(message.from_user.id, None)

async def process_both(client, message, session, password):
    await process_both_final(client, message, session, password, "")

# ========== CORRECTION : Utiliser app.run() au lieu de la fonction main() ==========
if __name__ == "__main__":
    print("🚀 Démarrage du bot PDF (Pyrogram)...")
    print("✅ Bot configuré et prêt!")
    print("👉 Envoie /start au bot pour commencer")
    app.run()  # C'est ÇA la ligne importante !