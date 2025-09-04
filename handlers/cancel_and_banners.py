"""
Handlers pour les commandes /cancel et /deletebanner
"""
import asyncio
import shutil
import os
from pathlib import Path
from typing import List, Optional
import logging

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from utils.tasks import cancel_user_tasks, get_active_tasks_count

logger = logging.getLogger(__name__)

# Dossier où /setbanner enregistre vos bannières
BANNERS_ROOT = Path("data/banners")

def user_banner_dir(user_id: int) -> Path:
    """Retourne le dossier des bannières d'un utilisateur."""
    return BANNERS_ROOT / str(user_id)

def list_user_banners(user_id: int) -> List[Path]:
    """Liste toutes les bannières d'un utilisateur.
    Combine les fichiers dans data/banners/<uid>/ et la bannière par défaut enregistrée via /setbanner.
    """
    found: List[Path] = []
    # 1) Multi-banners under data/banners/<uid>
    d = user_banner_dir(user_id)
    if d.exists():
        found.extend([p for p in d.iterdir() if p.is_file()])
    # 2) Single default banner saved via /setbanner (in pdf.py -> BANNERS_DIR)
    try:
        from pdf import get_user_pdf_settings
        bp = get_user_pdf_settings(user_id).get("banner_path")
        if bp and os.path.exists(bp):
            found.append(Path(bp))
    except Exception:
        pass
    # Deduplicate by absolute path
    uniq = []
    seen = set()
    for p in found:
        ap = str(p.resolve()) if p.exists() else str(p)
        if ap not in seen:
            uniq.append(p)
            seen.add(ap)
    return sorted(uniq, key=lambda x: x.name.lower())

def delete_banner_by_index(user_id: int, index: int) -> Optional[Path]:
    """Supprime une bannière par son index."""
    files = list_user_banners(user_id)
    if 1 <= index <= len(files):
        target = files[index - 1]
        try:
            # If this is the default settings banner, clear setting too
            try:
                from pdf import get_user_pdf_settings, update_user_pdf_settings
                bp = get_user_pdf_settings(user_id).get("banner_path")
                if bp and os.path.abspath(bp) == os.path.abspath(str(target)):
                    update_user_pdf_settings(user_id, banner_path=None)
            except Exception:
                pass
            target.unlink(missing_ok=True)
            logger.info(f"🗑️ Bannière supprimée: {target.name} pour l'utilisateur {user_id}")
            return target
        except Exception as e:
            logger.error(f"Erreur lors de la suppression de la bannière: {e}")
            return None
    return None

def delete_all_banners(user_id: int) -> bool:
    """Supprime toutes les bannières d'un utilisateur."""
    d = user_banner_dir(user_id)
    ok = True
    if d.exists():
        try:
            shutil.rmtree(d)
            logger.info(f"🗑️ Toutes les bannières multi ont été supprimées pour l'utilisateur {user_id}")
        except Exception as e:
            logger.error(f"Erreur lors de la suppression des bannières: {e}")
            ok = False
    # Also remove the single default banner file and clear setting
    try:
        from pdf import get_user_pdf_settings, update_user_pdf_settings
        bp = get_user_pdf_settings(user_id).get("banner_path")
        if bp and os.path.exists(bp):
            try:
                os.remove(bp)
            except Exception:
                ok = False
        update_user_pdf_settings(user_id, banner_path=None)
    except Exception:
        pass
    return ok

@Client.on_message(filters.command(["cancel"]) & filters.private)
async def cmd_cancel(client: Client, message: Message) -> None:
    """
    Commande /cancel - Annule toutes les opérations en cours
    """
    user_id = message.from_user.id
    
    # Vérifier d'abord s'il y a des tâches actives
    active_count = get_active_tasks_count(user_id)
    
    if active_count == 0:
        await message.reply_text(
            "ℹ️ **Aucune opération en cours**\n\n"
            "Il n'y a rien à annuler pour le moment.",
            parse_mode="Markdown"
        )
        return
    
    # Message d'annulation en cours
    status_msg = await message.reply_text(
        f"⏳ **Annulation en cours...**\n\n"
        f"Arrêt de {active_count} opération(s)...",
        parse_mode="Markdown"
    )
    
    # Annuler les tâches
    cancelled = await cancel_user_tasks(user_id)
    
    # Mettre à jour le message avec le résultat
    if cancelled > 0:
        await status_msg.edit_text(
            f"✅ **Opération(s) annulée(s) avec succès!**\n\n"
            f"• {cancelled} tâche(s) stoppée(s)\n"
            f"• Vous pouvez maintenant lancer une nouvelle opération",
            parse_mode="Markdown"
        )
        
        # Nettoyer aussi la session si nécessaire
        from pdf import sessions, clear_processing_flag
        if user_id in sessions:
            clear_processing_flag(user_id, source="cancel", reason="user_cancel")
            sessions[user_id].pop('batch_mode', None)
            sessions[user_id].pop('batch_files', None)
    else:
        await status_msg.edit_text(
            "ℹ️ **Aucune opération active trouvée**\n\n"
            "Toutes les opérations étaient déjà terminées.",
            parse_mode="Markdown"
        )

@Client.on_message(filters.command(["deletebanner", "deletbanner"]) & filters.private)
async def cmd_deletebanner(client: Client, message: Message) -> None:
    """
    Commande /deletebanner - Supprime les bannières enregistrées
    Usage:
        /deletebanner         -> liste les bannières
        /deletebanner 2       -> supprime la bannière #2
        /deletebanner all     -> supprime toutes les bannières
    """
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    arg = args[1].strip().lower() if len(args) > 1 else None
    
    # Supprimer toutes les bannières
    if arg in {"all", "tout", "tous", "toutes"}:
        files = list_user_banners(user_id)
        if not files:
            await message.reply_text(
                "😶 **Aucune bannière à supprimer**\n\n"
                "Vous n'avez pas de bannières enregistrées.",
                parse_mode="Markdown"
            )
            return
        
        # Demander confirmation avec des boutons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Oui, tout supprimer", callback_data=f"delban_all_{user_id}"),
                InlineKeyboardButton("❌ Annuler", callback_data="delban_cancel")
            ]
        ])
        
        await message.reply_text(
            f"⚠️ **Confirmation requise**\n\n"
            f"Êtes-vous sûr de vouloir supprimer **{len(files)} bannière(s)** ?\n"
            f"Cette action est irréversible.",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return
    
    # Supprimer une bannière spécifique
    if arg and arg.isdigit():
        idx = int(arg)
        files = list_user_banners(user_id)
        
        if not files:
            await message.reply_text(
                "😶 **Aucune bannière enregistrée**\n\n"
                "Utilisez /setbanner pour ajouter une bannière.",
                parse_mode="Markdown"
            )
            return
        
        if idx < 1 or idx > len(files):
            await message.reply_text(
                f"❌ **Index invalide**\n\n"
                f"Veuillez choisir un nombre entre 1 et {len(files)}.",
                parse_mode="Markdown"
            )
            return
        
        deleted = delete_banner_by_index(user_id, idx)
        if deleted:
            remaining = len(list_user_banners(user_id))
            await message.reply_text(
                f"🗑️ **Bannière supprimée avec succès!**\n\n"
                f"• Fichier: `{deleted.name}`\n"
                f"• Bannières restantes: {remaining}",
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(
                "❌ **Erreur lors de la suppression**\n\n"
                "Impossible de supprimer cette bannière.",
                parse_mode="Markdown"
            )
        return
    
    # Lister les bannières
    files = list_user_banners(user_id)
    if not files:
        await message.reply_text(
            "😶 **Aucune bannière enregistrée**\n\n"
            "Utilisez `/setbanner` pour ajouter une bannière.",
            parse_mode="Markdown"
        )
        return
    
    # Créer la liste formatée
    listing = "\n".join(f"`{i+1}.` {p.name[:30]}{'...' if len(p.name) > 30 else ''}" 
                       for i, p in enumerate(files))
    
    # Créer les boutons pour supprimer
    buttons = []
    for i in range(0, len(files), 3):  # 3 boutons par ligne
        row = []
        for j in range(i, min(i+3, len(files))):
            row.append(InlineKeyboardButton(
                f"🗑️ #{j+1}", 
                callback_data=f"delban_{j+1}_{user_id}"
            ))
        buttons.append(row)
    
    # Ajouter le bouton "Tout supprimer"
    buttons.append([
        InlineKeyboardButton("🗑️ Tout supprimer", callback_data=f"delban_all_{user_id}")
    ])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    await message.reply_text(
        f"📂 **Vos bannières enregistrées** ({len(files)}):\n\n"
        f"{listing}\n\n"
        "Cliquez sur un bouton pour supprimer:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

@Client.on_callback_query(filters.regex(r"^delban_"))
async def callback_delete_banner(client: Client, callback_query):
    """Gère les callbacks pour la suppression de bannières."""
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    # Annuler
    if data == "delban_cancel":
        await callback_query.message.edit_text(
            "❌ **Suppression annulée**",
            parse_mode="Markdown"
        )
        return
    
    # Supprimer tout
    if data.startswith("delban_all_"):
        target_user = int(data.split("_")[2])
        if user_id != target_user:
            await callback_query.answer("❌ Cette action n'est pas pour vous!", show_alert=True)
            return
        
        if delete_all_banners(user_id):
            await callback_query.message.edit_text(
                "🗑️ **Toutes les bannières ont été supprimées!**\n\n"
                "Vous pouvez ajouter de nouvelles bannières avec `/setbanner`.",
                parse_mode="Markdown"
            )
        else:
            await callback_query.message.edit_text(
                "❌ **Erreur lors de la suppression**\n\n"
                "Impossible de supprimer les bannières.",
                parse_mode="Markdown"
            )
        return
    
    # Supprimer une bannière spécifique
    if data.count("_") == 2:
        _, idx_str, target_user_str = data.split("_")
        idx = int(idx_str)
        target_user = int(target_user_str)
        
        if user_id != target_user:
            await callback_query.answer("❌ Cette action n'est pas pour vous!", show_alert=True)
            return
        
        deleted = delete_banner_by_index(user_id, idx)
        if deleted:
            remaining = len(list_user_banners(user_id))
            await callback_query.message.edit_text(
                f"🗑️ **Bannière #{idx} supprimée!**\n\n"
                f"• Fichier: `{deleted.name}`\n"
                f"• Bannières restantes: {remaining}",
                parse_mode="Markdown"
            )
        else:
            await callback_query.message.edit_text(
                "❌ **Erreur lors de la suppression**",
                parse_mode="Markdown"
            )


