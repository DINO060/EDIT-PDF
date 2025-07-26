import asyncio
import logging
import signal
import sys
from pathlib import Path
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, ApiIdInvalid
from config.settings import settings
from config.logging_config import setup_logging
from services.state_manager import state_manager
from bot.middlewares import middleware_manager
from handlers.errors import ErrorHandler

logger = logging.getLogger(__name__)

class PDFBotClient:
    """Production-ready PDF bot client"""
    
    def __init__(self):
        self.client = None
        self.running = False
        self.shutdown_event = asyncio.Event()
        self.health_status = {"status": "initializing"}
        
    async def initialize(self):
        """Initialize the bot client"""
        try:
            logger.info("Initializing PDF Bot Client...")
            
            # Validate settings
            settings.validate()
            
            # Ensure directories exist
            Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
            
            # Initialize Pyrogram client
            self.client = Client(
                "pdf_bot",
                api_id=settings.API_ID,
                api_hash=settings.API_HASH,
                bot_token=settings.BOT_TOKEN,
                plugins=dict(root="handlers"),
                workdir="data"
            )
            
            # Set up signal handlers
            self._setup_signal_handlers()
            
            logger.info("Bot client initialized successfully")
            self.health_status = {"status": "initialized"}
            
        except Exception as e:
            self.health_status = {"status": "failed", "error": str(e)}
            logger.error(f"Failed to initialize bot client: {e}")
            raise
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating shutdown...")
            asyncio.create_task(self.shutdown())
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    async def start(self):
        """Start the bot"""
        try:
            if not self.client:
                await self.initialize()
            
            logger.info("Starting PDF Processing Bot...")
            
            # Start the client
            await self.client.start()
            
            # Get bot info
            me = await self.client.get_me()
            logger.info(f"Bot started successfully: @{me.username} (ID: {me.id})")
            
            # Start background tasks
            await self._start_background_tasks()
            
            # Update health status
            self.health_status = {
                "status": "running",
                "bot_id": me.id,
                "username": me.username,
                "started_at": asyncio.get_event_loop().time()
            }
            
            self.running = True
            
            # Wait for shutdown signal
            await self.shutdown_event.wait()
            
        except AuthKeyUnregistered:
            logger.error("Bot token is invalid or revoked")
            self.health_status = {"status": "auth_failed", "error": "Invalid bot token"}
            raise
        except ApiIdInvalid:
            logger.error("API ID or API Hash is invalid")
            self.health_status = {"status": "auth_failed", "error": "Invalid API credentials"}
            raise
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            self.health_status = {"status": "error", "error": str(e)}
            raise
        finally:
            await self._cleanup()
    
    async def _start_background_tasks(self):
        """Start background maintenance tasks"""
        # Start state manager cleanup
        state_manager.start_cleanup_task()
        
        # Start health monitoring
        asyncio.create_task(self._health_monitor())
        
        logger.info("Background tasks started")
    
    async def _health_monitor(self):
        """Monitor bot health"""
        while self.running:
            try:
                # Check if client is still connected
                if self.client and not self.client.is_connected:
                    logger.warning("Bot client disconnected, attempting reconnection...")
                    await self.client.start()
                
                # Update health metrics
                self.health_status["last_check"] = asyncio.get_event_loop().time()
                self.health_status["active_sessions"] = len(state_manager.get_all_sessions())
                
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                logger.error(f"Health monitor error: {e}")
                self.health_status["health_error"] = str(e)
                await asyncio.sleep(60)  # Wait longer on error
    
    async def shutdown(self):
        """Graceful shutdown"""
        if not self.running:
            return
        
        logger.info("Initiating graceful shutdown...")
        self.running = False
        
        try:
            # Stop accepting new requests
            self.health_status["status"] = "shutting_down"
            
            # Clean up background tasks
            state_manager.stop_cleanup_task()
            
            # Clean up all user sessions
            logger.info("Cleaning up user sessions...")
            for user_id in list(state_manager.sessions.keys()):
                await state_manager.clear_user_session(user_id)
            
            # Stop the client
            if self.client:
                await self.client.stop()
                logger.info("Bot client stopped")
            
            self.health_status["status"] = "stopped"
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            self.health_status["status"] = "shutdown_error"
            self.health_status["error"] = str(e)
        finally:
            self.shutdown_event.set()
    
    async def _cleanup(self):
        """Final cleanup"""
        try:
            # Clean up temporary files
            import shutil
            if Path(settings.TEMP_DIR).exists():
                shutil.rmtree(settings.TEMP_DIR, ignore_errors=True)
            
            logger.info("Cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    
    def get_health_status(self) -> dict:
        """Get current health status"""
        return self.health_status.copy()
    
    async def restart(self):
        """Restart the bot"""
        logger.info("Restarting bot...")
        await self.shutdown()
        await asyncio.sleep(2)  # Brief pause
        await self.start()

# Global bot client instance
bot_client = PDFBotClient() 