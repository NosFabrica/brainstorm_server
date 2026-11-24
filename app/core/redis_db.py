import redis.asyncio as redis

from app.core.config import settings

# A connection the network dropped must raise, not wait forever: the consumers
# reconnect on an error. Longer than their longest blocking read (30 s).
SOCKET_TIMEOUT_SECONDS = 60


def get_redis_client():
    return redis.Redis(
        host=settings.redis_host,
        port=int(settings.redis_port),
        db=0,
        decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=10,
        socket_keepalive=True,
        health_check_interval=30,
    )


redis_client = get_redis_client()
