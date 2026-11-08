"""Everything that moves money. Each function is safe to call twice."""
import uuid

from django.db import IntegrityError, transaction

from . import clients
from .models import Customer, Order, Payment


class NotEnoughBalance(Exception):
    pass


class UnknownPayment(Exception):
    pass


class AmountMismatch(Exception):
    pass


def place_order(customer_id, amount, *, use_balance):
    """Pay from the balance, or create a payment to be paid by card."""
    with transaction.atomic():
        if not use_balance:
            order = Order.objects.create(
                customer_id=customer_id, amount=amount, status=Order.Status.AWAITING_PAYMENT
            )
            Payment.objects.create(order=order, amount=amount)
            return order

        # Lock the customer's row. A second order for the same customer waits
        # here until this transaction commits, then reads the balance that's left.
        customer = Customer.objects.select_for_update().get(pk=customer_id)
        if customer.balance < amount:
            raise NotEnoughBalance
        customer.balance -= amount
        customer.save(update_fields=["balance"])
        order = Order.objects.create(
            customer=customer, amount=amount, paid_from_balance=True, status=Order.Status.PAID
        )
        transaction.on_commit(lambda: _send_to_pos(order.pk))
        return order


def confirm_payment(payment_id, provider_txn_id, amount):
    """Apply a "paid" callback. Returns True if it changed anything, False for a replay."""
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().select_related("order").get(pk=payment_id)
        except Payment.DoesNotExist:
            raise UnknownPayment from None
        if payment.status != Payment.Status.PENDING:
            return False  # already applied: same answer, no second effect
        if amount != payment.amount:
            raise AmountMismatch

        payment.status = Payment.Status.PAID
        payment.provider_txn_id = provider_txn_id
        try:
            with transaction.atomic():  # savepoint, so the outer transaction survives
                payment.save(update_fields=["status", "provider_txn_id"])
        except IntegrityError:
            # This provider payment is already recorded on another row.
            # The unique constraint caught what the code above couldn't.
            return False

        order = payment.order
        order.status = Order.Status.PAID
        order.save(update_fields=["status"])
        transaction.on_commit(lambda: _send_to_pos(order.pk))
        return True


def refund(payment_id):
    """Refund a card payment once, however many times someone presses the button."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.status == Payment.Status.REFUNDED:
            return False
        if payment.status != Payment.Status.PAID:
            raise ValueError(f"cannot refund a {payment.status} payment")
        # Save our refund id BEFORE calling the provider. If the call times out
        # and someone tries again, the retry sends the same id and the provider
        # sees a repeat, not a second refund.
        if payment.refund_txn_id is None:
            payment.refund_txn_id = uuid.uuid4()
            payment.save(update_fields=["refund_txn_id"])

    # The call happens outside the row lock: holding a lock open during a slow
    # HTTP call would block everyone else, and if this process died mid-call the
    # saved refund id would be rolled back with it. The price: two people pressing
    # at the same moment can both call the provider. They send the same refund id,
    # so the provider treats it as one refund.
    error = None
    try:
        clients.refund(payment.provider_txn_id, payment.amount, str(payment.refund_txn_id))
    except clients.OutsideError as e:
        error = e

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if error is None:
            payment.status = Payment.Status.REFUNDED
            payment.error = ""
        else:
            # Save the error and commit it, then raise. Raising inside the
            # transaction would roll the message back too.
            payment.error = str(error)
        payment.save(update_fields=["status", "error"])
    if error is not None:
        raise error
    return True


def _send_to_pos(order_id):
    from .tasks import send_order_to_pos

    send_order_to_pos.delay(order_id)
