# 🚀 Guide de Démarrage Rapide - Bot PDF Manager

## ✅ Fonctionnalités Implémentées

### 1. **Support Dual Client** ✅
- **API Telegram** : Pour fichiers < 50MB (plus rapide)
- **Pyrogram** : Pour fichiers > 50MB (client haute performance)
- **Détection automatique** : Le bot choisit le bon client selon la taille

### 2. **Ajout Automatique de Username** ✅
- **Watermark** : Ajoute le username en filigrane sur chaque page
- **Page dédiée** : Ajoute une page à la fin avec le username et la date
- **Nom de fichier** : Nettoie et ajoute le username à la fin du nom
- **Exemple** : `[大人 - 42] I'm The Leader Of A Cult.pdf` → `[大人 - 42] I'm The Leader Of A Cult @PornhwaE.pdf`

### 3. **Nettoyage Automatique** ✅
- **Sessions** : Expirent après 7 jours d'inactivité
- **Fichiers temporaires** : Supprimés automatiquement
- **Périodicité** : Tous les 7 jours en arrière-plan

## 📋 Installation

### 1. Installer les dépendances
```bash
pip install -r requirements.txt
```

### 2. Configurer le bot
Éditez `config.py` avec vos tokens :
```python
API_ID = "votre_api_id"
API_HASH = "votre_api_hash"
BOT_TOKEN = "votre_bot_token"
```

### 3. Démarrer le bot
```bash
python pdf.py
```

## 🎯 Utilisation

### Ajouter un Username
1. Envoyez `/start` au bot
2. Cliquez sur "⚙️ Paramètre"
3. Cliquez sur "➕ Add Username"
4. Envoyez `[@username]` ou `[📢 @username]`

### Traiter un PDF
1. Envoyez un fichier PDF au bot
2. Le bot détecte automatiquement la taille et choisit le client approprié
3. Choisissez l'action :
   - **🔓 Déverrouiller** : Déverrouille un PDF protégé
   - **🗑️ Supprimer des pages** : Supprime des pages spécifiques
   - **🛠️ The Both** : Action combinée (déverrouiller + nettoyer + username)

### Exemples de Pages
- `1` → Supprime la page 1
- `1,3,5` → Supprime les pages 1, 3 et 5
- `1-5` → Supprime les pages 1 à 5

## 🔧 Fonctionnalités Avancées

### Support Dual Client
- **< 50MB** : API Telegram (traitement rapide)
- **> 50MB** : Pyrogram (client haute performance)
- **Détection automatique** : Aucune intervention manuelle requise

### Ajout Automatique de Username
- **Watermark** sur chaque page (si ReportLab installé)
- **Page dédiée** à la fin du PDF avec username et date
- **Nom de fichier** nettoyé et personnalisé
- **Gestion des formats** : `[@username]`, `[📢 @username]`, `@username`

### Nettoyage Automatique
- **Sessions** : Expirent après 7 jours
- **Fichiers temporaires** : Supprimés automatiquement
- **Logs détaillés** : Suivi du nettoyage dans les logs

## 🐛 Dépannage

### Erreur "Dépendances manquantes"
```bash
pip install -r requirements.txt --upgrade
```

### Erreur "Configuration invalide"
Vérifiez vos tokens dans `config.py`

### Erreur "ReportLab non disponible"
```bash
pip install reportlab==4.0.9
```

### Bot ne répond pas
1. Vérifiez que le bot est démarré
2. Vérifiez vos tokens
3. Vérifiez la connexion Internet

## 📊 Logs et Monitoring

Le bot génère des logs détaillés :
```
🚀 Démarrage du bot PDF avec double client...
📄 API Telegram pour fichiers < 50MB
📦 Pyrogram pour fichiers > 50MB
🧹 Nettoyage automatique activé (tous les 7 jours)
```

## 🎉 Résumé des Améliorations

✅ **Support dual client** (API Telegram + Pyrogram)  
✅ **Ajout automatique de username** (watermark + page dédiée + nom de fichier)  
✅ **Nettoyage automatique** (tous les 7 jours)  
✅ **Interface intuitive** avec boutons  
✅ **Gestion d'erreurs** robuste  
✅ **Logs détaillés** pour le monitoring  

## 🚀 C'est parti !

Votre bot est maintenant prêt avec toutes les fonctionnalités demandées. Il gère automatiquement :

- **Fichiers < 50MB** : API Telegram (rapide)
- **Fichiers > 50MB** : Pyrogram (haute performance)
- **Username** : Ajouté automatiquement (watermark + page + nom)
- **Nettoyage** : Automatique tous les 7 jours

**Bon usage ! 🎉** 