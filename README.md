# Telegram PDF Bot - Production Ready

Un bot Telegram avancé pour le traitement de fichiers PDF avec architecture modulaire, rate limiting avancé, et déploiement Docker.

## 🚀 Fonctionnalités

### Core Features
- **Traitement PDF avancé** : Déverrouillage, suppression de pages, watermark, traitement par lot
- **Architecture FSM** : Machine à états finis pour une gestion robuste des sessions utilisateur
- **Rate Limiting** : 5 algorithmes différents (Fixed Window, Sliding Window, Token Bucket, etc.)
- **Système multi-tiers** : Free, Premium, Admin, Banned avec limites différenciées
- **Anti-spam** : Protection avancée contre le spam avec scoring ML-like

### Production Features
- **Monitoring** : Intégration Prometheus/Grafana pour les métriques
- **Health Checks** : Vérifications automatiques de santé
- **Graceful Shutdown** : Arrêt propre avec nettoyage des ressources
- **Docker Ready** : Containerisation complète avec Docker Compose
- **Sécurité** : Exécution non-root, validation d'entrée, nettoyage automatique

## 📁 Architecture

```
Pdfbot/
├── bot/                    # Logique principale du bot
│   ├── __init__.py
│   ├── client.py          # PDFBotClient principal
│   ├── middlewares.py     # Rate limiting, logging, anti-spam
│   └── handlers/          # Gestionnaires d'événements
│       └── __init__.py
├── services/              # Services backend
│   ├── __init__.py
│   └── rate_limiter.py   # Rate limiting avancé
├── config/                # Configuration
│   ├── __init__.py
│   ├── settings.py       # Variables d'environnement
│   └── logging_config.py # Configuration logging
├── docker/               # Scripts Docker
│   ├── entrypoint.sh
│   └── healthcheck.sh
├── monitoring/           # Configuration monitoring
│   └── prometheus.yml
├── data/                 # Données persistantes
├── logs/                 # Fichiers de log
├── main.py              # Point d'entrée
├── requirements.txt      # Dépendances Python
├── Dockerfile           # Image Docker
├── docker-compose.yml   # Orchestration
└── README.md
```

## 🛠️ Installation

### Prérequis
- Python 3.11+
- Docker & Docker Compose
- Redis (optionnel pour le développement local)
- PostgreSQL (optionnel pour le développement local)

### 1. Cloner le projet
```bash
git clone <repository-url>
cd Pdfbot
```

### 2. Configuration
Créer un fichier `.env` à la racine :
```env
# Telegram Bot Configuration
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
ADMIN_IDS=123456789

# Channel Configuration
CHANNEL_USERNAME=@your_channel
CHANNEL_ID=-1001234567890

# Redis Configuration
REDIS_URL=redis://localhost:6379/0

# Database Configuration
DATABASE_URL=postgresql://pdfbot:password@localhost:5432/pdfbot
```

### 3. Installation locale
```bash
# Installer les dépendances
pip install -r requirements.txt

# Lancer le bot
python main.py
```

### 4. Déploiement Docker
```bash
# Lancer tous les services
docker-compose up -d

# Lancer avec monitoring
docker-compose --profile monitoring up -d

# Lancer avec proxy
docker-compose --profile proxy up -d
```

## 🔧 Configuration

### Variables d'environnement

| Variable | Description | Défaut |
|----------|-------------|---------|
| `API_ID` | Telegram API ID | - |
| `API_HASH` | Telegram API Hash | - |
| `BOT_TOKEN` | Bot Token | - |
| `ADMIN_IDS` | IDs des administrateurs | - |
| `REDIS_URL` | URL Redis | `redis://localhost:6379/0` |
| `DATABASE_URL` | URL PostgreSQL | `postgresql://pdfbot:password@localhost:5432/pdfbot` |
| `RATE_LIMIT_REQUESTS` | Limite de requêtes par fenêtre | `30` |
| `RATE_LIMIT_WINDOW` | Taille de fenêtre (secondes) | `60` |
| `AUTO_DELETE_DELAY` | Délai de suppression auto (secondes) | `300` |

### Rate Limiting

Le bot utilise 5 algorithmes de rate limiting :

1. **Fixed Window** : Fenêtre fixe avec compteur
2. **Sliding Window** : Fenêtre glissante avec sets triés
3. **Token Bucket** : Bucket de tokens avec recharge
4. **Leaky Bucket** : Bucket fuyant
5. **GCRA** : Generic Cell Rate Algorithm

### Système de tiers

- **FREE** : 30 messages/min, 10 uploads/5min
- **PREMIUM** : 100 messages/min, 50 uploads/5min
- **ADMIN** : 1000 messages/min, 500 uploads/5min
- **BANNED** : Accès bloqué

## 📊 Monitoring

### Métriques disponibles
- Nombre de messages traités
- Taux de succès/échec
- Utilisation des ressources
- Statistiques de rate limiting
- Temps de réponse

### Accès aux dashboards
- **Prometheus** : http://localhost:9090
- **Grafana** : http://localhost:3000 (admin/admin)

## 🚀 Utilisation

### Commandes principales
- `/start` : Démarrer le bot
- `/batch` : Mode traitement par lot
- `/process` : Traiter les fichiers en lot

### Fonctionnalités PDF
- **Déverrouillage** : Envoyer un PDF protégé par mot de passe
- **Suppression de pages** : Spécifier les pages à supprimer
- **Watermark** : Ajouter un watermark personnalisé
- **Traitement par lot** : Jusqu'à 24 fichiers simultanément

## 🔒 Sécurité

- **Exécution non-root** : Container s'exécute en tant qu'utilisateur dédié
- **Validation d'entrée** : Toutes les entrées sont validées et nettoyées
- **Nettoyage automatique** : Suppression automatique des fichiers temporaires
- **Rate limiting** : Protection contre les abus
- **Anti-spam** : Détection et blocage du spam

## 🐛 Dépannage

### Logs
```bash
# Voir les logs du bot
docker-compose logs pdf-bot

# Voir les logs Redis
docker-compose logs redis

# Voir les logs PostgreSQL
docker-compose logs postgres
```

### Health Checks
```bash
# Vérifier l'état des services
docker-compose ps

# Vérifier la santé du bot
curl http://localhost:8080/health
```

### Problèmes courants

1. **Erreur de connexion Redis**
   - Vérifier que Redis est démarré
   - Vérifier l'URL Redis dans `.env`

2. **Erreur d'authentification Telegram**
   - Vérifier `API_ID`, `API_HASH`, `BOT_TOKEN`
   - S'assurer que le bot est activé

3. **Erreur de permissions**
   - Vérifier les permissions des dossiers `data/` et `logs/`
   - S'assurer que l'utilisateur a les droits d'écriture

## 📈 Performance

### Optimisations incluses
- **Connection pooling** : Réutilisation des connexions Redis/PostgreSQL
- **Async/await** : Traitement asynchrone pour haute concurrence
- **Cache Redis** : Mise en cache des sessions et métadonnées
- **Nettoyage automatique** : Suppression des données expirées

### Métriques de performance
- **Temps de réponse** : < 2s pour les opérations simples
- **Concurrence** : Support de 100+ utilisateurs simultanés
- **Mémoire** : < 512MB par instance
- **Stockage** : Nettoyage automatique des fichiers temporaires

## 🤝 Contribution

1. Fork le projet
2. Créer une branche feature (`git checkout -b feature/AmazingFeature`)
3. Commit les changements (`git commit -m 'Add AmazingFeature'`)
4. Push vers la branche (`git push origin feature/AmazingFeature`)
5. Ouvrir une Pull Request

## 📄 Licence

Ce projet est sous licence MIT. Voir le fichier `LICENSE` pour plus de détails.

## 🆘 Support

- **Issues** : Utiliser les GitHub Issues pour les bugs
- **Discussions** : GitHub Discussions pour les questions
- **Documentation** : Voir les commentaires dans le code

---

**Note** : Ce bot est conçu pour un usage en production avec une architecture scalable et maintenable. 