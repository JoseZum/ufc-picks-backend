"""
Instancia compartida del rate limiter (slowapi).
Se importa desde main.py y los controllers para evitar imports circulares.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    storage_uri="memory://",
)
