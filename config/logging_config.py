import logging
import logging.handlers
import os
from config.settings import LOG_LEVEL, LOG_FILE

def setup_logging():
    """Setup logging configuration for the bot"""
    
    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            # Console handler
            logging.StreamHandler(),
            # File handler with rotation
            logging.handlers.RotatingFileHandler(
                LOG_FILE,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
        ]
    )
    
    # Set specific loggers
    loggers = [
        'pyrogram',
        'bot',
        'services',
        'handlers'
    ]
    
    for logger_name in loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(getattr(logging, LOG_LEVEL.upper()))
    
    # Create custom logger for bot
    bot_logger = logging.getLogger('bot')
    bot_logger.info("Logging configured successfully")
    
    return bot_logger

def get_logger(name: str) -> logging.Logger:
    """Get a logger with the specified name"""
    return logging.getLogger(name) 