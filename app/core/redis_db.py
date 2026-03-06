import redis.asyncio as redis
from app.core.config import settings

redis_client = redis.Redis(
    host=settings.redis_host, port=settings.redis_port, db=0, decode_responses=True
)


def get_redis_client():
    return redis.Redis(
        host=settings.redis_host, port=settings.redis_port, db=0, decode_responses=True
    )
