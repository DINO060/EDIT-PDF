# Configuration publique du bot Telegram PDF
# Ce fichier peut être partagé sur GitHub (sans clés privées)

# Configuration du bot
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# Messages du bot
MESSAGES = {
    'start': "🤖 Bot PDF Manager prêt à l'emploi!\nEnvoie-moi un PDF pour commencer.",
    'not_pdf': "❌ Ce n'est pas un fichier PDF !",
    'file_too_big': "❌ Fichier trop volumineux !",
    'processing': "⏳ Traitement en cours...",
    'success_unlock': "✅ PDF déverrouillé avec succès !",
    'success_pages': "✅ Pages supprimées avec succès !",
    'error': "❌ Erreur lors du traitement"
}

# Instructions pour configurer le bot :
# 1. Copier ce fichier vers config.py
# 2. Ajouter vos clés API privées :
#    API_ID = votre_api_id
#    API_HASH = "votre_api_hash"
#    BOT_TOKEN = "votre_bot_token"
#    ADMIN_IDS = "votre_admin_ids"
