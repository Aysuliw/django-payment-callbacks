import json
import threading
from unittest import mock

import requests
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from . import clients, services, signing
from .models import Customer, Order, Payment
from .tasks import order_external_id, send_order_to_pos


def run_in_parallel(fn, times):
    """Run fn in `times` threads that start together, each with its own DB connection."""
    barrier = threading.Barrier(times)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(fn())
        except Exception as e:  # noqa: BLE001 - collected and checked by the test
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def fake_response(status, body=None):
    res = requests.Response()
    res.status_code = status
    res._content = json.dumps(body or {}).encode()
    return res


def callback(client, payment, txn_id="txn-1", amount=None, signature=None):
    body = json.dumps(
        {"payment_id": payment.pk, "transaction_id": txn_id, "amount": amount or payment.amount}
    ).encode()
    return client.post(
        "/payments/callback/",
        body,
        content_type="application/json",
        headers={"X-Signature": signature if signature is not None else signing.sign(body)},
    )


def card_order(amount=25_000):
    customer = Customer.objects.create(name="A")
    return services.place_order(customer.pk, amount, use_balance=False).payment


@mock.patch("payments.services._send_to_pos")
class CallbackTests(TestCase):
    def test_paid_callback_marks_payment_and_order_paid(self, send):
        payment = card_order()
        with self.captureOnCommitCallbacks(execute=True):
            res = callback(self.client, payment)
        self.assertEqual(res.json(), {"success": True})
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(payment.order.status, Order.Status.PAID)
        send.assert_called_once_with(payment.order.pk)

    def test_replay_answers_success_and_changes_nothing(self, send):
        payment = card_order()
        with self.captureOnCommitCallbacks(execute=True):
            callback(self.client, payment)
            res = callback(self.client, payment)
        self.assertEqual(res.json(), {"success": True})
        send.assert_called_once()

    def test_bad_signature_is_refused(self, send):
        payment = card_order()
        res = callback(self.client, payment, signature="0" * 64)
        self.assertEqual(res.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_wrong_amount_is_refused(self, send):
        payment = card_order()
        res = callback(self.client, payment, amount=1)
        self.assertEqual(res.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_same_provider_payment_cannot_pay_two_orders(self, send):
        first, second = card_order(), card_order()
        callback(self.client, first, txn_id="txn-same")
        res = callback(self.client, second, txn_id="txn-same")
        self.assertEqual(res.json(), {"success": True})
        second.refresh_from_db()
        self.assertEqual(second.status, Payment.Status.PENDING)

    def test_database_refuses_duplicate_provider_txn(self, send):
        first, second = card_order(), card_order()
        Payment.objects.filter(pk=first.pk).update(provider_txn_id="txn-x")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.filter(pk=second.pk).update(provider_txn_id="txn-x")


@mock.patch("payments.services._send_to_pos")
class ParallelTests(TransactionTestCase):
    """Real concurrent transactions. These fail without select_for_update."""

    def test_parallel_duplicate_callbacks_apply_once(self, send):
        payment = card_order()
        results, errors = run_in_parallel(
            lambda: services.confirm_payment(payment.pk, "txn-1", payment.amount), times=8
        )
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False] * 7 + [True])
        send.assert_called_once_with(payment.order.pk)

    def test_parallel_orders_cannot_overspend_balance(self, send):
        customer = Customer.objects.create(name="A", balance=20_000)
        results, errors = run_in_parallel(
            lambda: services.place_order(customer.pk, 20_000, use_balance=True), times=5
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(isinstance(e, services.NotEnoughBalance) for e in errors))
        customer.refresh_from_db()
        self.assertEqual(customer.balance, 0)

    def test_parallel_refunds_all_send_one_refund_id(self, send):
        # The provider call happens outside the row lock (see services.refund),
        # so parallel presses may each call it. They must all send the same id,
        # which the provider treats as one refund.
        payment = card_order()
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.PAID, provider_txn_id="t")
        with mock.patch("payments.clients.refund") as refund:
            _, errors = run_in_parallel(lambda: services.refund(payment.pk), times=5)
        self.assertEqual(errors, [])
        refund_ids = {c.args[2] for c in refund.call_args_list}
        self.assertEqual(len(refund_ids), 1)
        payment.refresh_from_db()
        self.assertEqual(refund_ids, {str(payment.refund_txn_id)})
        self.assertEqual(payment.status, Payment.Status.REFUNDED)

    def test_refund_after_success_does_nothing(self, send):
        payment = card_order()
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.REFUNDED, provider_txn_id="t")
        with mock.patch("payments.clients.refund") as refund:
            self.assertFalse(services.refund(payment.pk))
        refund.assert_not_called()


class SendToPosTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name="A", balance=0)
        self.order = Order.objects.create(customer=self.customer, amount=5_000, status=Order.Status.PAID)

    def test_external_id_is_stable(self):
        self.assertEqual(order_external_id(self.order.pk), order_external_id(self.order.pk))
        self.assertNotEqual(order_external_id(self.order.pk), order_external_id(self.order.pk + 1))

    def test_retry_after_timeout_sends_the_same_id(self):
        sent_ids = []

        def post(url, json, timeout):
            sent_ids.append(json["id"])
            if len(sent_ids) == 1:
                raise requests.Timeout("slow")
            return fake_response(200)

        with mock.patch("payments.clients.requests.post", side_effect=post):
            send_order_to_pos.apply(args=[self.order.pk])
        self.assertEqual(len(sent_ids), 2)
        self.assertEqual(sent_ids[0], sent_ids[1])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.SENT)

    def test_5xx_is_retried(self):
        with mock.patch("payments.clients.requests.post", side_effect=[fake_response(502), fake_response(200)]) as post:
            send_order_to_pos.apply(args=[self.order.pk])
        self.assertEqual(post.call_count, 2)

    def test_4xx_is_not_retried_and_balance_comes_back(self):
        Order.objects.filter(pk=self.order.pk).update(paid_from_balance=True)
        with mock.patch("payments.clients.requests.post", return_value=fake_response(400)) as post:
            send_order_to_pos.apply(args=[self.order.pk])
        self.assertEqual(post.call_count, 1)
        self.order.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REJECTED)
        self.assertEqual(self.customer.balance, 5_000)

    def test_429_waits_inside_the_client(self):
        waits = []
        responses = [fake_response(429), fake_response(429), fake_response(200)]
        with mock.patch("payments.clients.requests.post", side_effect=responses):
            clients.send_order("id", self.order, sleep=waits.append)
        self.assertEqual(waits, [5, 15])


class RefundTests(TestCase):
    def setUp(self):
        self.payment = card_order()
        Payment.objects.filter(pk=self.payment.pk).update(status=Payment.Status.PAID, provider_txn_id="t")

    def test_failed_refund_keeps_its_id_and_the_error(self):
        with mock.patch("payments.clients.refund", side_effect=clients.Unavailable("timeout")):
            with self.assertRaises(clients.Unavailable):
                services.refund(self.payment.pk)
        self.payment.refresh_from_db()
        first_id = self.payment.refund_txn_id
        self.assertIsNotNone(first_id)
        self.assertEqual(self.payment.error, "timeout")

        with mock.patch("payments.clients.refund") as refund:
            services.refund(self.payment.pk)
        self.assertEqual(refund.call_args.args[2], str(first_id))  # same id on the retry
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(self.payment.error, "")
