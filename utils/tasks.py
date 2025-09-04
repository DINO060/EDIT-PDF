"""
Gestion centralisée des tâches en cours pour permettre l'annulation avec /cancel
"""
import asyncio
from collections import defaultdict
from typing import Dict, Set
import logging

logger = logging.getLogger(__name__)

# { user_id: {asyncio.Task, ...} }
ACTIVE_TASKS: Dict[int, Set[asyncio.Task]] = defaultdict(set)

def register_task(user_id: int, task: asyncio.Task) -> None:
    """Enregistre une tâche lancée pour un utilisateur et attache le cleanup."""
    try:
        if task is None:
            return
        ACTIVE_TASKS[user_id].add(task)
        logger.info(f"📝 Tâche enregistrée pour l'utilisateur {user_id}")

        def _cleanup(t: asyncio.Task) -> None:
            ACTIVE_TASKS[user_id].discard(t)
            if not ACTIVE_TASKS[user_id]:
                ACTIVE_TASKS.pop(user_id, None)
            logger.info(f"🧹 Tâche nettoyée pour l'utilisateur {user_id}")

        task.add_done_callback(_cleanup)
    except Exception as e:
        logger.debug(f"register_task error: {e}")

async def cancel_user_tasks(user_id: int) -> int:
    """Annule toutes les tâches en cours pour un utilisateur et attend leur arrêt."""
    try:
        tasks = list(ACTIVE_TASKS.get(user_id, set()))
        cancelled = 0

        for t in tasks:
            if not t.done():
                t.cancel()
                cancelled += 1
                logger.info(f"❌ Tâche annulée pour l'utilisateur {user_id}")

        if cancelled:
            # Empêche la propagation d'exceptions d'annulation
            await asyncio.gather(*tasks, return_exceptions=True)

        ACTIVE_TASKS.pop(user_id, None)
        return cancelled
    except Exception as e:
        logger.debug(f"cancel_user_tasks error: {e}")
        return 0

def get_active_tasks_count(user_id: int) -> int:
    """Retourne le nombre de tâches actives pour un utilisateur."""
    try:
        return len([t for t in ACTIVE_TASKS.get(user_id, set()) if not t.done()])
    except Exception:
        return 0


