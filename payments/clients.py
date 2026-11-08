"""Clients for the two outside systems: the point-of-sale system that receives
paid orders, and the payment provider's refund API.

Errors are split by what a retry can fix:
  Unavailable  5xx, timeouts, connection errors: worth retrying
  Rejected     4xx: our request is wrong, repeating it won't help
429 is handled here, by waiting, so callers never see it unless it persists.
"""
import time

import requests
from django.conf import settings

RATE_LIMIT_WAITS = (5, 15, 30)


class OutsideError(Exception):
    pass


class Unavailable(OutsideError):
    pass


class Rejected(OutsideError):
    pass


def _post(url, payload, sleep=time.sleep):
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            res = requests.post(url, json=payload, timeout=settings.OUTSIDE_TIMEOUT)
        except requests.RequestException as e:
            raise Unavailable(f"{e.__class__.__name__}: {e}") from e
        if res.status_code == 429 and wait is not None:
            sleep(wait)
            continue
        break
    if res.status_code >= 500 or res.status_code == 429:
        raise Unavailable(f"HTTP {res.status_code}")
    if res.status_code >= 400:
        raise Rejected(f"HTTP {res.status_code}: {res.text[:200]}")
    return res.json() if res.content else {}


def send_order(external_id, order, sleep=time.sleep):
    url = settings.POS_URL.rstrip("/") + "/orders"
    return _post(url, {"id": external_id, "amount": order.amount}, sleep=sleep)


def refund(provider_txn_id, amount, refund_id, sleep=time.sleep):
    """Ask the payment provider to refund. refund_id is our idempotency key:
    the provider treats a second request with the same id as the same refund."""
    url = settings.PROVIDER_URL.rstrip("/") + "/refunds"
    payload = {"payment": provider_txn_id, "amount": amount, "refund_id": refund_id}
    return _post(url, payload, sleep=sleep)
