import asyncio
import functools
import logging
import time

logger = logging.getLogger(__name__)

async def retry_async(func, *args, retries=5, delay=1, backoff=2, exceptions=(Exception,), **kwargs):
    """
    Retry an async function with exponential backoff.
    """
    attempt = 0
    while True:
        try:
            return await func(*args, **kwargs)
        except exceptions as e:
            attempt += 1
            if attempt > retries:
                logger.error(f"Max retries reached for {func.__name__}: {e}")
                raise
            logger.warning(f"Retry {attempt}/{retries} for {func.__name__} due to {e}. Retrying in {delay} seconds...")
            await asyncio.sleep(delay)
            delay *= backoff

def retryable(retries=5, delay=1, backoff=2, exceptions=(Exception,)):
    """
    Decorator to make an async function retryable with exponential backoff.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_async(func, *args, retries=retries, delay=delay, backoff=backoff, exceptions=exceptions, **kwargs)
        return wrapper
    return decorator
