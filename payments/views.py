import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services, signing


@csrf_exempt  # called by the provider's servers; the signature is the protection
@require_POST
def payment_callback(request):
    if not signing.is_valid(request.body, request.headers.get("X-Signature", "")):
        return JsonResponse({"success": False, "error": "bad signature"}, status=403)
    try:
        data = json.loads(request.body)
        payment_id = int(data["payment_id"])
        txn_id = str(data["transaction_id"])
        amount = int(data["amount"])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"success": False, "error": "bad request"}, status=400)

    try:
        services.confirm_payment(payment_id, txn_id, amount)
    except services.UnknownPayment:
        return JsonResponse({"success": False, "error": "unknown payment"}, status=404)
    except services.AmountMismatch:
        return JsonResponse({"success": False, "error": "amount mismatch"}, status=400)
    # A replay gets exactly the same answer as the first delivery. For many
    # providers a failure answer means "reverse the payment", so answering an
    # honest duplicate with an error would refund a customer who really paid.
    return JsonResponse({"success": True})


@csrf_exempt  # demo endpoint without auth; a real app would check the user
@require_POST
def create_order(request):
    try:
        data = json.loads(request.body)
        order = services.place_order(
            int(data["customer_id"]), int(data["amount"]), use_balance=bool(data.get("use_balance"))
        )
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "bad request"}, status=400)
    except services.NotEnoughBalance:
        return JsonResponse({"error": "not enough balance"}, status=409)
    body = {"order": order.pk, "status": order.status}
    if hasattr(order, "payment"):
        body["payment"] = order.payment.pk
    return JsonResponse(body, status=201)
