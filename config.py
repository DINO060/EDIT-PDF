# Configuration du bot Telegram PDF
# Remplace les valeurs par tes propres clés API

# Clés Telegram (obtenues sur https://my.telegram.org)
API_ID = 22142337  # Remplace par ton API_ID
API_HASH = "18eef60818a0a15ab2e8bcaeac66698e"  # Remplace par ton API_HASH
BOT_TOKEN = "8172549450:AAGs8OlyIe5j7SF_Xm5k_eHnEj5PWKz_-SY"  # Remplace par ton BOT_TOKEN
ADMIN_IDS = "7570539064,7615697178"  # IDs des admins sous forme de chaîne, séparés par des virgules

# Configuration du bot
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GB

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