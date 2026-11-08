import uuid

from celery import shared_task

from . import clients
from .models import Customer, Order

# Fixed namespace for this app's order ids. Never change it: that would give
# every existing order a new id.
ORDER_NAMESPACE = uuid.UUID("6f1c2a3e-9b7d-4c1e-8a52-3d0b4e7f9a10")


def order_external_id(order_id):
    """Same order -> same id, on the first try and on every retry."""
    return str(uuid.uuid5(ORDER_NAMESPACE, f"order-{order_id}"))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_to_pos(self, order_id):
    order = Order.objects.get(pk=order_id)
    if order.status != Order.Status.PAID:
        return  # already sent or rejected
    try:
        clients.send_order(order_external_id(order.pk), order)
    except clients.Unavailable as e:
        # Temporary. The next attempt sends the same id, so if this one
        # did arrive after all, the POS sees a repeat, not a new order.
        raise self.retry(exc=e)
    except clients.Rejected as e:
        # Our request is wrong; repeating it won't help. Record why, and give
        # back balance this order spent so no money stays stuck.
        _reject(order_id, str(e))
        return
    Order.objects.filter(pk=order_id, status=Order.Status.PAID).update(status=Order.Status.SENT)


def _reject(order_id, error):
    from django.db import transaction
    from django.db.models import F

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status != Order.Status.PAID:
            return
        order.status = Order.Status.REJECTED
        order.error = error
        order.save(update_fields=["status", "error"])
        if order.paid_from_balance:
            Customer.objects.filter(pk=order.customer_id).update(balance=F("balance") + order.amount)
