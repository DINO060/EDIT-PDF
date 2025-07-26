#!/usr/bin/env python3
"""
Main entry point for the Telegram PDF Bot
Production-ready with proper error handling and graceful shutdown
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add the project root to Python path
sys.path.insert(0, str(Path(__file__).parent))

from config.logging_config import setup_logging
from config.settings import settings
from bot.client import bot_client
from services.rate_limiter import rate_limiter

async def main():
    """Main application entry point"""
    try:
        # Setup logging
        logger = setup_logging()
        logger.info("Starting PDF Bot application...")
        
        # Validate configuration
        settings.validate()
        logger.info("Configuration validated successfully")
        
        # Initialize rate limiter
        await rate_limiter.initialize()
        logger.info("Rate limiter initialized")
        
        # Start the bot
        await bot_client.start()
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)
    finally:
        # Cleanup
        try:
            await rate_limiter.close()
            logger.info("Cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

if __name__ == "__main__":
    # Run the main application
    asyncio.run(main()) 