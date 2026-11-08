# django-payment-callbacks

A small, complete example of **money-safe payment handling in Django**: replayed callbacks,
parallel orders, retries to an outside system and refunds, each one safe to happen twice.

The write-up explaining each decision:
**[Idempotent payment callbacks in Django](https://icedev.uz/writing/idempotent-payment-callbacks.html)**.

The payment provider and the point-of-sale system are fakes behind plain HTTP calls,
so the code shows the patterns, not one provider's API.

## What's in it

| Problem | Where | What stops it |
|---|---|---|
| Two orders spend the same balance at once | `services.place_order` | `select_for_update` on the customer row |
| The provider sends the same "paid" callback twice | `services.confirm_payment`, `views.payment_callback` | Lock the payment, check its status, answer a replay with the same `success: true` |
| Forged callbacks | `signing.py` | HMAC-SHA256 checked with `hmac.compare_digest` |
| Code that forgets the lock | `models.Payment` | `UniqueConstraint` on the provider's transaction id |
| A retry after a timeout creates a second order | `tasks.send_order_to_pos` | `uuid5` order id, the same on every attempt |
| Retrying errors that can't succeed | `clients.py` | Retry 5xx and network errors; never 4xx; wait on 429 |
| Double refunds | `services.refund` | Lock, check, save the refund id **before** calling the provider |

## Tests that can fail

16 tests on real PostgreSQL. The `ParallelTests` start up to 8 threads at the same moment,
each with its own database connection:

- 8 identical callbacks → the payment is applied once, the order is sent once;
- 5 orders spending the same balance → one succeeds, four get "not enough balance";
- 5 refund presses → one refund id reaches the provider.

Remove the `select_for_update()` calls from `payments/services.py` and these tests fail.

## Run it

Needs Docker and Python 3.11+.

```bash
docker compose up -d                       # Postgres 16 on port 55433
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py test
```

## Trade-offs

- **The refund call happens outside the row lock.** Holding a lock during a slow HTTP call
  blocks everyone else, and if the process died mid-call the saved refund id would roll back
  with it. So two people pressing "refund" at the same moment can both reach the provider,
  but with the same refund id, which the provider treats as one refund. This relies on the
  provider supporting idempotency keys; most do.
- **Orders are sent to the POS after commit** (`transaction.on_commit`), so a rolled-back
  payment never produces an order. If the worker is down at that moment the task waits in the broker.
- **Amounts are integers** in the smallest currency unit. No floats near money.

## License

MIT
