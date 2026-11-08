import hashlib
import hmac

from django.conf import settings


def sign(body: bytes) -> str:
    return hmac.new(settings.PROVIDER_SECRET.encode(), body, hashlib.sha256).hexdigest()


def is_valid(body: bytes, signature: str) -> bool:
    # compare_digest takes the same time wherever the strings differ,
    # so response timing doesn't reveal how much of a forged signature was right.
    return hmac.compare_digest(sign(body), signature or "")
