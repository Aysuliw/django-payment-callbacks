from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=100)
    # Amounts are integers in the smallest currency unit. No floats near money.
    balance = models.PositiveBigIntegerField(default=0)


class Order(models.Model):
    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment"
        PAID = "paid"
        SENT = "sent"  # the point-of-sale system accepted it
        REJECTED = "rejected"  # the point-of-sale system refused it for good

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    amount = models.PositiveBigIntegerField()
    paid_from_balance = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices)
    error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PAID = "paid"
        REFUNDED = "refunded"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="payment")
    amount = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Set by the provider's callback. Unique, so the database itself refuses to
    # record one provider payment twice, whatever the code above it does.
    provider_txn_id = models.CharField(max_length=64, null=True, blank=True)
    # Our id for the refund. Saved BEFORE the refund call, reused on every retry.
    refund_txn_id = models.UUIDField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [
            # NULLs don't clash in a unique index, so pending payments are fine.
            models.UniqueConstraint(fields=["provider_txn_id"], name="unique_provider_txn"),
        ]
