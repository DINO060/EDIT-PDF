# Configuration du bot Telegram PDF - EXEMPLE
# Copie ce fichier vers config.py et remplace les valeurs par tes propres clés API

# Clés Telegram (obtenues sur https://my.telegram.org)
API_ID = 12345678  # Remplace par ton API_ID
API_HASH = "abcdef1234567890abcdef1234567890"  # Remplace par ton API_HASH
BOT_TOKEN = ""  # Remplace par ton BOT_TOKEN

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