# Bot Telegram PDF Manager

Un bot Telegram puissant pour gérer les PDF : déverrouillage, suppression de pages, nettoyage de texte et ajout de watermarks.

## 🚀 Fonctionnalités

- **🔓 Déverrouillage de PDF** : Supprime la protection par mot de passe
- **🗑️ Suppression de pages** : Supprime des pages spécifiques ou automatiques (première, dernière, milieu)
- **🧹 Nettoyage automatique** : Supprime les @username et hashtags du contenu
- **📝 Watermark personnalisé** : Ajoute ton @username en filigrane
- **🛠️ The Both** : Action combinée (déverrouillage + suppression + nettoyage + watermark)
- **📁 Gestion des noms de fichiers** : Nettoie et ajoute automatiquement ton @username

## ⚙️ Configuration

### 1. Obtenir les clés Telegram

1. Va sur [https://my.telegram.org](https://my.telegram.org)
2. Connecte-toi avec ton numéro de téléphone
3. Va dans "API development tools"
4. Crée une nouvelle application
5. Note tes clés : `API_ID`, `API_HASH`, et `BOT_TOKEN`

### 2. Configurer le bot

1. Copie `config_example.py` vers `config.py`
2. Remplace les valeurs dans `config.py` par tes clés :
   ```python
   API_ID = 12345678  # Ton API_ID
   API_HASH = "abcdef1234567890abcdef1234567890"  # Ton API_HASH
   BOT_TOKEN = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"  # Ton BOT_TOKEN
   ```

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
```

### 4. Lancer le bot

```bash
python pdf.py
```

## 📖 Utilisation

### Commandes principales

- `/start` : Démarrer le bot et voir les options
- **⚙️ Paramètre** : Configurer ton @username personnalisé
- **🔓 Déverrouiller** : Supprimer la protection d'un PDF
- **🗑️ Supprimer des pages** : Enlever des pages spécifiques
- **🛠️ The Both** : Action combinée complète

### Configuration du username

1. Clique sur **⚙️ Paramètre**
2. Clique sur **➕ Add Username**
3. Envoie ton @username : `@TonChannel` ou `[📢 @TonChannel]`
4. Le bot l'ajoutera automatiquement aux noms de fichiers

### Exemples d'utilisation

**Déverrouiller un PDF :**
1. Envoie un PDF protégé
2. Clique sur **🔓 Déverrouiller**
3. Envoie le mot de passe
4. Reçois le PDF déverrouillé avec ton @username

**Supprimer la première page :**
1. Envoie un PDF
2. Clique sur **🗑️ Supprimer des pages**
3. Clique sur **The first page**
4. Reçois le PDF sans la première page

**The Both (action complète) :**
1. Envoie un PDF protégé
2. Clique sur **🛠️ The Both**
3. Envoie le mot de passe
4. Choisis les pages à supprimer
5. Reçois le PDF traité (déverrouillé + pages supprimées + nettoyé + watermark)

## 🔒 Sécurité

- Le fichier `config.py` est dans `.gitignore` pour éviter d'exposer tes clés
- Les sessions Pyrogram sont également ignorées
- Les fichiers temporaires sont automatiquement nettoyés

## 📝 Structure des fichiers

```
Pdfbot/
├── pdf.py              # Script principal du bot
├── config.py           # Configuration (clés API) - IGNORÉ par Git
├── config_example.py   # Exemple de configuration
├── requirements.txt    # Dépendances Python
├── .gitignore         # Fichiers ignorés par Git
└── README.md          # Ce fichier
```

## 🛠️ Dépendances

- `pyrogram` : Client Telegram
- `PyPDF2` : Manipulation de PDF
- `reportlab` : Création de watermarks (optionnel)

## 📞 Support

Si tu rencontres des problèmes :
1. Vérifie que tes clés API sont correctes
2. Assure-toi d'avoir installé toutes les dépendances
3. Vérifie les logs dans la console pour les erreurs

## ⚠️ Note importante

Ce bot est conçu pour un usage personnel. Respecte les droits d'auteur et les conditions d'utilisation de Telegram. 