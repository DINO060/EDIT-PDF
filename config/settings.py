import os
from dotenv import load_dotenv
load_dotenv()

# Telegram Bot Configuration
API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "your_api_hash")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token")
ADMIN_IDS = os.getenv("ADMIN_IDS", "123456789")

# Channel Configuration
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@your_channel")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001234567890"))

# Rate Limiting Configuration
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_CALLBACKS = int(os.getenv("RATE_LIMIT_CALLBACKS", "60"))
RATE_LIMIT_UPLOADS = int(os.getenv("RATE_LIMIT_UPLOADS", "10"))
RATE_LIMIT_PROCESSING = int(os.getenv("RATE_LIMIT_PROCESSING", "5"))

# File Management
TEMP_DIR = os.getenv("TEMP_DIR", "temp_files")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
AUTO_DELETE_DELAY = int(os.getenv("AUTO_DELETE_DELAY", "300"))  # 5 minutes

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Database Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://pdfbot:password@localhost:5432/pdfbot")

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")

# Anti-spam Configuration
SPAM_THRESHOLD = int(os.getenv("SPAM_THRESHOLD", "10"))
SPAM_WINDOW = int(os.getenv("SPAM_WINDOW", "60"))

# PDF Processing Configuration
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "50"))  # MB
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "24"))
MAX_PAGES_PER_PDF = int(os.getenv("MAX_PAGES_PER_PDF", "100"))

# Monitoring Configuration
METRICS_PORT = int(os.getenv("METRICS_PORT", "8080"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "30"))

def validate():
    """Validate required configuration"""
    required_vars = ["API_ID", "API_HASH", "BOT_TOKEN"]
    missing_vars = []
    
    for var in required_vars:
        if not globals().get(var) or globals()[var] in ["", "your_api_hash", "your_bot_token"]:
            missing_vars.append(var)
    
    if missing_vars:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    # Ensure directories exist
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    os.makedirs("logs", exist_ok=True) 