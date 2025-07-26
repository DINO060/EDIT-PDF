import asyncio
import time
import logging
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from enum import Enum
from typing import Dict, Tuple, Optional, Any
from dataclasses import dataclass
import redis.asyncio as redis
import json
from config.settings import REDIS_URL, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW

logger = logging.getLogger(__name__)

class UserTier(Enum):
    FREE = "free"
    PREMIUM = "premium"
    ADMIN = "admin"
    BANNED = "banned"

class RateLimitStrategy(Enum):
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"
    TOKEN_BUCKET = "token_bucket"
    LEAKY_BUCKET = "leaky_bucket"
    GCRA = "gcra"  # Generic Cell Rate Algorithm

@dataclass
class RateLimitConfig:
    requests_per_window: int
    window_size: int  # seconds
    burst_size: int = 0
    strategy: RateLimitStrategy = RateLimitStrategy.FIXED_WINDOW

@dataclass
class UserLimits:
    message_limit: RateLimitConfig
    callback_limit: RateLimitConfig
    upload_limit: RateLimitConfig
    processing_limit: RateLimitConfig

class RateLimitAlgorithm(ABC):
    """Abstract base class for rate limiting algorithms"""
    
    @abstractmethod
    async def is_allowed(self, key: str, config: RateLimitConfig) -> Tuple[bool, int]:
        """Check if request is allowed. Returns (allowed, wait_time)"""
        pass
    
    @abstractmethod
    async def cleanup(self):
        """Cleanup expired data"""
        pass

class FixedWindowRateLimit(RateLimitAlgorithm):
    """Fixed window rate limiting"""
    
    def __init__(self, redis_client):
        self.redis = redis_client
    
    async def is_allowed(self, key: str, config: RateLimitConfig) -> Tuple[bool, int]:
        current_time = int(time.time())
        window_start = current_time - (current_time % config.window_size)
        window_key = f"rate_limit:fixed:{key}:{window_start}"
        
        try:
            pipe = self.redis.pipeline()
            pipe.incr(window_key)
            pipe.expire(window_key, config.window_size)
            results = await pipe.execute()
            
            current_count = results[0]
            
            if current_count <= config.requests_per_window:
                return True, 0
            else:
                wait_time = config.window_size - (current_time % config.window_size)
                return False, wait_time
                
        except Exception as e:
            logger.error(f"Redis error in fixed window rate limit: {e}")
            return True, 0  # Fail open
    
    async def cleanup(self):
        # Cleanup is handled by Redis TTL
        pass

class SlidingWindowRateLimit(RateLimitAlgorithm):
    """Sliding window rate limiting using sorted sets"""
    
    def __init__(self, redis_client):
        self.redis = redis_client
    
    async def is_allowed(self, key: str, config: RateLimitConfig) -> Tuple[bool, int]:
        current_time = time.time()
        window_start = current_time - config.window_size
        sorted_set_key = f"rate_limit:sliding:{key}"
        
        try:
            pipe = self.redis.pipeline()
            # Remove old entries
            pipe.zremrangebyscore(sorted_set_key, 0, window_start)
            # Count current entries
            pipe.zcard(sorted_set_key)
            # Add current request
            pipe.zadd(sorted_set_key, {str(current_time): current_time})
            # Set expiry
            pipe.expire(sorted_set_key, config.window_size)
            
            results = await pipe.execute()
            current_count = results[1]
            
            if current_count < config.requests_per_window:
                return True, 0
            else:
                # Calculate wait time based on oldest entry
                oldest_entries = await self.redis.zrange(sorted_set_key, 0, 0, withscores=True)
                if oldest_entries:
                    oldest_time = oldest_entries[0][1]
                    wait_time = int(config.window_size - (current_time - oldest_time)) + 1
                    return False, max(1, wait_time)
                return False, config.window_size
                
        except Exception as e:
            logger.error(f"Redis error in sliding window rate limit: {e}")
            return True, 0  # Fail open
    
    async def cleanup(self):
        # Cleanup is handled by zremrangebyscore
        pass

class TokenBucketRateLimit(RateLimitAlgorithm):
    """Token bucket rate limiting"""
    
    def __init__(self, redis_client):
        self.redis = redis_client
    
    async def is_allowed(self, key: str, config: RateLimitConfig) -> Tuple[bool, int]:
        bucket_key = f"rate_limit:bucket:{key}"
        current_time = time.time()
        
        # Lua script for atomic token bucket operations
        lua_script = """
        local bucket_key = KEYS[1]
        local max_tokens = tonumber(ARGV[1])
        local refill_rate = tonumber(ARGV[2])
        local current_time = tonumber(ARGV[3])
        local tokens_requested = tonumber(ARGV[4])
        
        local bucket = redis.call('HMGET', bucket_key, 'tokens', 'last_refill')
        local tokens = tonumber(bucket[1]) or max_tokens
        local last_refill = tonumber(bucket[2]) or current_time
        
        -- Calculate tokens to add
        local time_passed = current_time - last_refill
        local tokens_to_add = time_passed * refill_rate
        tokens = math.min(max_tokens, tokens + tokens_to_add)
        
        if tokens >= tokens_requested then
            tokens = tokens - tokens_requested
            redis.call('HMSET', bucket_key, 'tokens', tokens, 'last_refill', current_time)
            redis.call('EXPIRE', bucket_key, 3600)
            return {1, 0}  -- allowed, wait_time
        else
            redis.call('HMSET', bucket_key, 'tokens', tokens, 'last_refill', current_time)
            redis.call('EXPIRE', bucket_key, 3600)
            local wait_time = (tokens_requested - tokens) / refill_rate
            return {0, math.ceil(wait_time)}  -- not allowed, wait_time
        end
        """
        
        try:
            refill_rate = config.requests_per_window / config.window_size
            result = await self.redis.eval(
                lua_script, 1, bucket_key,
                config.requests_per_window, refill_rate, current_time, 1
            )
            
            return bool(result[0]), int(result[1])
            
        except Exception as e:
            logger.error(f"Redis error in token bucket rate limit: {e}")
            return True, 0
    
    async def cleanup(self):
        # Cleanup is handled by Redis TTL
        pass

class AdvancedRateLimiter:
    """Advanced rate limiter with multiple strategies and user tiers"""
    
    def __init__(self):
        self.redis_client = None
        self.algorithms = {}
        self.user_tiers = defaultdict(lambda: UserTier.FREE)
        self.user_penalties = defaultdict(int)
        self.penalty_expiry = defaultdict(float)
        
        # Define limits for different user tiers
        self.tier_limits = {
            UserTier.FREE: UserLimits(
                message_limit=RateLimitConfig(30, 60, 10),
                callback_limit=RateLimitConfig(60, 60, 20),
                upload_limit=RateLimitConfig(10, 300, 5),
                processing_limit=RateLimitConfig(5, 60, 2)
            ),
            UserTier.PREMIUM: UserLimits(
                message_limit=RateLimitConfig(100, 60, 30),
                callback_limit=RateLimitConfig(200, 60, 50),
                upload_limit=RateLimitConfig(50, 300, 20),
                processing_limit=RateLimitConfig(20, 60, 10)
            ),
            UserTier.ADMIN: UserLimits(
                message_limit=RateLimitConfig(1000, 60, 100),
                callback_limit=RateLimitConfig(2000, 60, 200), 
                upload_limit=RateLimitConfig(500, 300, 100),
                processing_limit=RateLimitConfig(100, 60, 50)
            )
        }
    
    async def initialize(self):
        """Initialize Redis connection and algorithms"""
        try:
            self.redis_client = redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True
            )
            
            # Test connection
            await self.redis_client.ping()
            
            # Initialize algorithms
            self.algorithms = {
                RateLimitStrategy.FIXED_WINDOW: FixedWindowRateLimit(self.redis_client),
                RateLimitStrategy.SLIDING_WINDOW: SlidingWindowRateLimit(self.redis_client),
                RateLimitStrategy.TOKEN_BUCKET: TokenBucketRateLimit(self.redis_client)
            }
            
            logger.info("Rate limiter initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize rate limiter: {e}")
            raise
    
    async def is_allowed(self, user_id: int, action_type: str, 
                        strategy: RateLimitStrategy = RateLimitStrategy.SLIDING_WINDOW) -> Tuple[bool, int, dict]:
        """Check if user action is allowed"""
        
        # Check if user is banned
        user_tier = self.user_tiers[user_id]
        if user_tier == UserTier.BANNED:
            return False, 86400, {"reason": "user_banned"}  # 24 hour wait
        
        # Check penalties
        current_time = time.time()
        if user_id in self.penalty_expiry and current_time < self.penalty_expiry[user_id]:
            remaining = int(self.penalty_expiry[user_id] - current_time)
            return False, remaining, {"reason": "penalty_active", "remaining": remaining}
        
        # Get user limits
        user_limits = self.tier_limits[user_tier]
        
        # Select appropriate limit config
        if action_type == "message":
            config = user_limits.message_limit
        elif action_type == "callback":
            config = user_limits.callback_limit
        elif action_type == "upload":
            config = user_limits.upload_limit
        elif action_type == "processing":
            config = user_limits.processing_limit
        else:
            config = user_limits.message_limit  # Default
        
        # Apply penalty multiplier
        penalty_level = self.user_penalties[user_id]
        if penalty_level > 0:
            # Reduce limits based on penalty level
            penalty_multiplier = max(0.1, 1.0 - (penalty_level * 0.2))
            config.requests_per_window = int(config.requests_per_window * penalty_multiplier)
        
        # Check rate limit using selected algorithm
        algorithm = self.algorithms[strategy]
        key = f"{user_id}:{action_type}"
        
        allowed, wait_time = await algorithm.is_allowed(key, config)
        
        # Update statistics
        await self._update_stats(user_id, action_type, allowed)
        
        if not allowed:
            # Apply penalty
            await self._apply_penalty(user_id)
        
        return allowed, wait_time, {
            "user_tier": user_tier.value,
            "penalty_level": penalty_level,
            "action_type": action_type,
            "strategy": strategy.value
        }
    
    async def _apply_penalty(self, user_id: int):
        """Apply progressive penalty to user"""
        current_penalty = self.user_penalties[user_id]
        new_penalty = min(current_penalty + 1, 10)  # Max 10 penalty levels
        
        self.user_penalties[user_id] = new_penalty
        
        # Set penalty expiry (exponential backoff)
        penalty_duration = min(300 * (2 ** new_penalty), 86400)  # Max 24 hours
        self.penalty_expiry[user_id] = time.time() + penalty_duration
        
        logger.warning(f"Applied penalty level {new_penalty} to user {user_id} for {penalty_duration}s")
    
    async def _update_stats(self, user_id: int, action_type: str, allowed: bool):
        """Update rate limiting statistics"""
        stats_key = f"rate_limit_stats:{user_id}:{action_type}"
        field = "allowed" if allowed else "blocked"
        
        try:
            await self.redis_client.hincrby(stats_key, field, 1)
            await self.redis_client.expire(stats_key, 86400)  # Keep stats for 24h
        except Exception as e:
            logger.error(f"Error updating rate limit stats: {e}")
    
    async def set_user_tier(self, user_id: int, tier: UserTier):
        """Set user tier"""
        self.user_tiers[user_id] = tier
        logger.info(f"Set user {user_id} tier to {tier.value}")
    
    async def get_user_stats(self, user_id: int) -> dict:
        """Get user rate limiting statistics"""
        stats = {}
        
        for action_type in ["message", "callback", "upload", "processing"]:
            stats_key = f"rate_limit_stats:{user_id}:{action_type}"
            try:
                action_stats = await self.redis_client.hgetall(stats_key)
                stats[action_type] = {
                    "allowed": int(action_stats.get("allowed", 0)),
                    "blocked": int(action_stats.get("blocked", 0))
                }
            except Exception:
                stats[action_type] = {"allowed": 0, "blocked": 0}
        
        return {
            "user_id": user_id,
            "user_tier": self.user_tiers[user_id].value,
            "penalty_level": self.user_penalties[user_id],
            "penalty_expires": self.penalty_expiry.get(user_id, 0),
            "stats": stats
        }
    
    async def get_global_stats(self) -> dict:
        """Get global rate limiting statistics"""
        try:
            keys = await self.redis_client.keys("rate_limit_stats:*")
            total_stats = defaultdict(lambda: {"allowed": 0, "blocked": 0})
            
            for key in keys:
                action_type = key.split(":")[-1]
                stats = await self.redis_client.hgetall(key)
                total_stats[action_type]["allowed"] += int(stats.get("allowed", 0))
                total_stats[action_type]["blocked"] += int(stats.get("blocked", 0))
            
            return dict(total_stats)
            
        except Exception as e:
            logger.error(f"Error getting global stats: {e}")
            return {}
    
    async def cleanup(self):
        """Cleanup expired data"""
        current_time = time.time()
        
        # Clean up expired penalties
        expired_users = [
            user_id for user_id, expiry_time in self.penalty_expiry.items()
            if current_time > expiry_time
        ]
        
        for user_id in expired_users:
            del self.penalty_expiry[user_id]
            self.user_penalties[user_id] = max(0, self.user_penalties[user_id] - 1)
            if self.user_penalties[user_id] == 0:
                del self.user_penalties[user_id]
        
        # Run algorithm cleanup
        for algorithm in self.algorithms.values():
            await algorithm.cleanup()
        
        if expired_users:
            logger.info(f"Cleaned up penalties for {len(expired_users)} users")
    
    async def close(self):
        """Close Redis connection"""
        if self.redis_client:
            await self.redis_client.close()

# Global rate limiter instance
rate_limiter = AdvancedRateLimiter() 