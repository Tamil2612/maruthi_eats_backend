"""
Maruthi Eats backend — Cloud Functions for Python (2nd gen).

Five functions, each solving one specific gap identified in the Flutter
app review:

1. on_order_created   — audits every order placed by the current client
                         flow (checkout_screen.dart writes to `orders`
                         directly) and flags anything that doesn't match
                         real menu/coupon data. Deploy this FIRST — it
                         requires zero Flutter changes.

2. place_order         — an optional, more secure replacement for the
                         client's direct Firestore write. The server
                         computes the price; the client can no longer
                         submit its own total. Requires a small Flutter
                         change (see README).

3. on_order_status_updated — sends a push notification to the customer
                         when order_status changes. Requires adding
                         firebase_messaging to the Flutter app and saving
                         an fcm_token on the user's doc (see README).

4. expire_promotions   — daily scheduled cleanup: flips is_active to
                         false on expired coupons/offers so the admin
                         dashboard reflects reality (the apps already
                         filter expired ones out client-side; this just
                         keeps the data itself honest).

5. razorpay_create_order / razorpay_verify_payment / razorpay_webhook /
   expire_unpaid_orders — UPI payments. An online order is created as
                         `pending_payment` (hidden from both apps, no admin
                         alert) and only becomes `placed` once the payment is
                         verified on the server (signature check from the app,
                         webhook as backup). Unpaid orders expire after 30 min.

Firestore schema assumed throughout — see order_validation.py docstring.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from firebase_admin import initialize_app, firestore, messaging
from firebase_functions import firestore_fn, https_fn, scheduler_fn, options, params
import razorpay

from order_validation import DELIVERY_FEE, price_items, validate_coupon, parse_int, parse_float

initialize_app()

# --- Secrets & config -------------------------------------------------
# Set these ONCE (test keys now, live keys when you go live):
#   firebase functions:secrets:set RAZORPAY_KEY_ID
#   firebase functions:secrets:set RAZORPAY_KEY_SECRET
#   firebase functions:secrets:set RAZORPAY_WEBHOOK_SECRET
# A secret is only available to a function that lists it in `secrets=[...]`
# on its decorator (see section 5). Without that the value is missing at
# runtime - the old code then silently fell back to a placeholder key and
# Razorpay rejected every call with "Authentication failed".
RAZORPAY_KEY_ID = params.SecretParam("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = params.SecretParam("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = params.SecretParam("RAZORPAY_WEBHOOK_SECRET")

# Unpaid online orders older than this are expired by expire_unpaid_orders.
PAYMENT_WINDOW_MINUTES = 30

REGION = "asia-south1"  # Mumbai — closest region to an Indian user base


def _notify_admin(order_id: str, order: dict) -> None:
    """Data-only, high-priority push to the admin app (topic `admin_orders`)."""
    items = order.get("items", [])
    try:
        total_val = order.get("total", 0)

        # DATA-ONLY on purpose. If a `notification` block is included, Android
        # shows its own small tray notification while the app is in the
        # background/closed and never runs the app's FCM handler - so the
        # full-screen alert + looping sound can't start. With data only, the
        # Flutter background handler always runs and builds the full-screen
        # notification itself. `priority="high"` wakes the device immediately.
        title_text = "\U0001F6A8 NEW ORDER RECEIVED!"
        body_text = f"Order #{order_id[:6].upper()} \u2022 \u20b9{total_val}"

        # Extra details for the full-screen alert (kept short: FCM data <= 4 KB).
        try:
            total_str = str(int(round(float(total_val))))
        except (TypeError, ValueError):
            total_str = str(total_val)
        item_lines = []
        for it in (items or []):
            if isinstance(it, dict):
                item_lines.append(f"{it.get('qty', 1)}x {it.get('name', 'Item')}")
        if len(item_lines) > 6:
            item_lines = item_lines[:6] + [f"+{len(item_lines) - 6} more"]
        items_text = "\n".join(item_lines)[:400]
        payment_text = "UPI" if order.get("payment_mode") == "upi" else "COD"

        messaging.send(messaging.Message(
            topic="admin_orders",
            data={
                "order_id": order_id,
                "status": "placed",
                "title": title_text,
                "body": body_text,
                "total": total_str,
                "items": items_text,
                "payment": payment_text,
            },
            android=messaging.AndroidConfig(
                priority="high",
                # Don't ring for an order alert that is already stale.
                ttl=timedelta(minutes=5),
            ),
            # iOS (if ever used) has no background-handler equivalent, so it
            # keeps a normal alert push with sound.
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        alert=messaging.ApsAlert(title=title_text, body=body_text),
                        sound="notification.mp3",
                        content_available=True,
                    )
                ),
            ),
        ))
        print(f"[FCM SENT] Admin notification sent for order {order_id}")
    except Exception as e:
        print(f"[FCM ERROR] Failed to send admin notification for order {order_id}: {e}")


# =======================================================================
# 1. Order audit trigger — no Flutter changes required
# =======================================================================
@firestore_fn.on_document_created(document="orders/{order_id}", region=REGION)
def on_order_created(event: firestore_fn.Event) -> None:
    db = firestore.client()
    order = event.data.to_dict()
    if order is None:
        return

    items = order.get("items", [])
    recomputed_item_total, problems = price_items(db, items)

    submitted_item_total = Decimal(str(order.get("item_total") or 0))
    if abs(recomputed_item_total - submitted_item_total) > Decimal('0.01'):
        problems.append(
            f"Submitted item_total {submitted_item_total} does not match "
            f"recomputed {recomputed_item_total}"
        )

    discount, coupon_error = validate_coupon(db, order.get("coupon_code"), recomputed_item_total)
    submitted_discount = Decimal(str(order.get("coupon_discount") or 0))
    if coupon_error:
        problems.append(coupon_error)
    elif abs(discount - submitted_discount) > Decimal('0.01'):
        problems.append(
            f"Submitted coupon_discount {submitted_discount} does not match "
            f"expected {discount}"
        )

    expected_total = max(Decimal('0'), recomputed_item_total - discount + Decimal(str(DELIVERY_FEE)))
    submitted_total = Decimal(str(order.get("total") or 0))
    if abs(expected_total - submitted_total) > Decimal('0.01'):
        problems.append(f"Submitted total {submitted_total} does not match expected {expected_total}")

    doc_ref = db.collection("orders").document(event.params["order_id"])
    if problems:
        doc_ref.update({
            "validation_status": "flagged",
            "validation_notes": problems,
        })
        print(f"[ORDER FLAGGED] {event.params['order_id']}: {problems}")
    else:
        doc_ref.update({"validation_status": "ok"})

    # Admin alert only for orders that are really placed (COD). Online orders
    # are announced once their payment is verified (see on_order_status_updated).
    if order.get("order_status", "placed") == "placed":
        _notify_admin(event.params["order_id"], order)


# =======================================================================
# 2. Secure order placement (optional — replaces the client's direct write)
# =======================================================================
@https_fn.on_call(region=REGION)
def place_order(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        data = req.data or {}
        items = data.get("items", [])  # [{item_id, name, qty, is_offer, is_free, ...}]
        coupon_code = data.get("coupon_code")

        if not items:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "Cart is empty")

        priced_items = []
        item_total = Decimal('0')
        for line in items:
            if not isinstance(line, dict):
                continue

            qty = parse_int(line.get("qty"))
            price = parse_float(line.get("price"))

            if line.get("is_free"):
                priced_items.append({**line, "price": 0, "qty": qty})
                continue
            if line.get("is_offer"):
                priced_items.append({**line, "price": price, "qty": qty})
                item_total += Decimal(str(price)) * qty
                continue

            item_id = line.get("item_id")
            if isinstance(item_id, dict):
                item_id = item_id.get("id") or item_id.get("item_id") or str(item_id)

            if not item_id:
                raise https_fn.HttpsError(
                    https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                    "Line item missing item_id",
                )

            menu_doc = db.collection("menu_items").document(str(item_id)).get()
            if not menu_doc.exists:
                raise https_fn.HttpsError(
                    https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                    f"Item {item_id} is no longer on the menu",
                )
            menu_item = menu_doc.to_dict() or {}
            if not menu_item.get("available", True):
                raise https_fn.HttpsError(
                    https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                    f"{menu_item.get('name', 'Item')} is currently unavailable",
                )
            real_price = parse_float(menu_item.get("discount_price") if menu_item.get("has_discount") else menu_item.get("price"))
            item_total += Decimal(str(real_price)) * qty
            priced_items.append({**line, "name": menu_item.get("name", ""), "price": real_price, "qty": qty})

        item_total_dec = item_total.quantize(Decimal('0.01'))
        discount_dec, coupon_error = validate_coupon(db, coupon_code, item_total_dec)
        if coupon_error:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, coupon_error)

        total_dec = max(Decimal('0'), item_total_dec - discount_dec + Decimal(str(DELIVERY_FEE)))

        item_total_val = float(item_total_dec)
        discount_val = float(discount_dec)
        total_val = float(total_dec)

        order_ref = db.collection("orders").document()
        order_ref.set({
            "customer_id": req.auth.uid,
            "items": priced_items,
            "item_total": item_total_val,
            "delivery_fee": float(DELIVERY_FEE),
            "coupon_code": coupon_code,
            "coupon_discount": discount_val,
            "total": total_val,
            "payment_mode": data.get("payment_mode", "cod"),
            "payment_status": "pending" if data.get("payment_mode") == "upi" else "cod_pending",
            "order_status": "pending_payment" if data.get("payment_mode") == "upi" else "placed",
            "delivery_address": data.get("delivery_address"),
            "address_label": data.get("address_label"),
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "validation_status": "ok",  # server-computed — no need for the audit trigger to re-check
            "created_at": firestore.SERVER_TIMESTAMP,
        })

        return {"order_id": order_ref.id, "total": total_val}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Server error during place_order: {type(e).__name__} - {str(e)}",
        )


# =======================================================================
# 3. Push notification on order status change
# =======================================================================
@firestore_fn.on_document_updated(document="orders/{order_id}", region=REGION)
def on_order_status_updated(event: firestore_fn.Event) -> None:
    before = event.data.before.to_dict() or {}
    after = event.data.after.to_dict() or {}

    before_status = before.get("order_status")
    after_status = after.get("order_status")
    if before_status == after_status:
        return  # nothing relevant changed

    # Payment verified -> the order is now real: ring the restaurant.
    if before_status == "pending_payment" and after_status == "placed":
        _notify_admin(event.params["order_id"], after)

    # Unpaid / expired online orders are invisible to customers - no push.
    if after_status in ("pending_payment", "payment_expired"):
        return

    customer_id = after.get("customer_id")
    if not customer_id:
        return

    db = firestore.client()
    user_doc = db.collection("users").document(customer_id).get()
    fcm_token = (user_doc.to_dict() or {}).get("fcm_token") if user_doc.exists else None
    if not fcm_token:
        return  # Flutter side hasn't saved a token for this user yet

    status_labels = {
        "placed": "Your order has been placed!",
        "confirmed": "Your order has been confirmed by the restaurant",
        "preparing": "Your food is being prepared",
        "out_for_delivery": "Your order is out for delivery",
        "delivered": "Your order has been delivered — enjoy!",
        "cancelled": "Your order was cancelled",
    }
    body = status_labels.get(after.get("order_status"), "Your order status has been updated")

    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(
                title="Maruthi Eats",
                body=body,
            ),
            data={
                "order_id": event.params["order_id"],
                "type": "order_status",
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="order_status_channel",
                    priority="high",
                    default_sound=True,
                    default_vibrate_timings=True,
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    )
                )
            ),
            token=fcm_token,
        ))
    except Exception as e:  # noqa: BLE001 — a bad/expired token shouldn't crash the function
        print(f"Failed to send notification to {customer_id}: {e}")


# =======================================================================
# 4. Daily coupon/offer expiry cleanup
# =======================================================================
@scheduler_fn.on_schedule(schedule="every day 00:00", region=REGION)
def expire_promotions(event: scheduler_fn.ScheduledEvent) -> None:
    from datetime import datetime, timezone

    db = firestore.client()
    now = datetime.now(timezone.utc)

    for collection_name in ("coupons", "offers"):
        expired = (
            db.collection(collection_name)
            .where("is_active", "==", True)
            .where("expiry_date", "<", now)
            .get()
        )
        for doc in expired:
            doc.reference.update({"is_active": False})
        if expired:
            print(f"Deactivated {len(expired)} expired docs in {collection_name}")


# =======================================================================
# 5. Payment — Razorpay (UPI)
#
# Flow:
#   place_order (UPI)            -> order_status "pending_payment" (hidden, no alert)
#   razorpay_create_order        -> Razorpay order for that Firestore order
#   app opens Razorpay checkout  -> user pays
#   razorpay_verify_payment      -> signature check -> "placed" + admin alert
#   razorpay_webhook             -> same result, in case the app was closed
#   expire_unpaid_orders         -> abandoned "pending_payment" -> "payment_expired"
# =======================================================================
def _secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def _razorpay_client():
    key_id = _secret("RAZORPAY_KEY_ID")
    key_secret = _secret("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        print("[RAZORPAY] RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
              "Run: firebase functions:secrets:set ... and redeploy.")
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            "Online payment is not set up on the server yet.",
        )
    return razorpay.Client(auth=(key_id, key_secret)), key_id


@firestore.transactional
def _mark_paid_txn(transaction, order_ref, payment_id):
    """Idempotent: the app call and the webhook can both arrive."""
    snap = order_ref.get(transaction=transaction)
    order = snap.to_dict() or {}

    if order.get("payment_status") == "paid":
        return "already_paid"

    if order.get("order_status") == "pending_payment":
        transaction.update(order_ref, {
            "payment_status": "paid",
            "order_status": "placed",
            "razorpay_payment_id": payment_id,
            "paid_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        return "paid"

    # Money arrived for an order that already expired/was cancelled: keep the
    # record so it can be refunded from the Razorpay dashboard.
    transaction.update(order_ref, {
        "payment_status": "paid_needs_refund",
        "razorpay_payment_id": payment_id,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    return "needs_refund"


def _find_order_by_razorpay_id(db, razorpay_order_id):
    if not razorpay_order_id:
        return None
    docs = (
        db.collection("orders")
        .where("razorpay_order_id", "==", razorpay_order_id)
        .limit(1)
        .get()
    )
    return docs[0].reference if docs else None


@https_fn.on_call(region=REGION, secrets=[RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET])
def razorpay_create_order(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        order_id = (req.data or {}).get("order_id")
        if not order_id:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "order_id is required")

        db = firestore.client()
        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        if order.get("customer_id") != req.auth.uid:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED, "Not your order")
        if order.get("payment_mode") != "upi":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                                      "This order is not an online-payment order")
        if order.get("payment_status") == "paid":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                                      "This order is already paid")
        if order.get("order_status") != "pending_payment":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                                      "This order can no longer be paid. Please place it again.")

        # Amount in paise, computed with Decimal (no float rounding surprises).
        amount_paise = int((Decimal(str(parse_float(order.get("total", 0)))) * 100).to_integral_value())
        if amount_paise < 100:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                                      "Online payment needs a total of at least ₹1")

        client, key_id = _razorpay_client()

        # Retry-safe: reuse the Razorpay order if we already made one for this amount.
        razorpay_order_id = order.get("razorpay_order_id")
        if not razorpay_order_id or order.get("razorpay_amount") != amount_paise:
            razorpay_order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": order_id,  # Firestore ids are 20 chars; Razorpay allows 40
                "notes": {"firestore_order_id": order_id},
            })
            razorpay_order_id = razorpay_order["id"]
            order_ref.update({
                "razorpay_order_id": razorpay_order_id,
                "razorpay_amount": amount_paise,
                "updated_at": firestore.SERVER_TIMESTAMP,
            })

        return {
            "razorpay_order_id": razorpay_order_id,
            "amount": amount_paise,
            "currency": "INR",
            "key_id": key_id,
        }
    except https_fn.HttpsError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()  # full detail goes to the function logs
        if "Authentication failed" in str(e):
            msg = "Payment gateway keys are not set up correctly."
        else:
            msg = f"Could not start payment ({type(e).__name__}). Please try again."
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INTERNAL, msg)


@https_fn.on_call(region=REGION, secrets=[RAZORPAY_KEY_SECRET])
def razorpay_verify_payment(req: https_fn.CallableRequest) -> dict:
    """Called by the app right after Razorpay reports success. The order only
    becomes `placed` if Razorpay's signature checks out - the app can no longer
    mark itself as paid."""
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    data = req.data or {}
    order_id = data.get("order_id")
    rzp_order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")
    if not all([order_id, rzp_order_id, payment_id, signature]):
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "Missing payment details")

    try:
        db = firestore.client()
        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        if order.get("customer_id") != req.auth.uid:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED, "Not your order")
        if order.get("razorpay_order_id") != rzp_order_id:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED,
                                      "Payment does not belong to this order")

        key_secret = _secret("RAZORPAY_KEY_SECRET")
        if not key_secret:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                                      "Online payment is not set up on the server yet.")
        expected = hmac.new(
            key_secret.encode(), f"{rzp_order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, str(signature)):
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED,
                                      "Payment signature could not be verified")

        status = _mark_paid_txn(db.transaction(), order_ref, payment_id)
        return {"status": status}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Could not confirm payment ({type(e).__name__}).",
        )


@https_fn.on_request(region=REGION, secrets=[RAZORPAY_WEBHOOK_SECRET])
def razorpay_webhook(req: https_fn.Request) -> https_fn.Response:
    webhook_secret = _secret("RAZORPAY_WEBHOOK_SECRET")
    if not webhook_secret:
        # Never accept unsigned webhooks.
        print("[RAZORPAY WEBHOOK] RAZORPAY_WEBHOOK_SECRET is not set")
        return https_fn.Response("Webhook not configured", status=500)

    signature = req.headers.get("X-Razorpay-Signature", "")
    body = req.get_data()
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return https_fn.Response("Invalid signature", status=400)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return https_fn.Response("Invalid JSON", status=400)

    event_type = payload.get("event")
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    rzp_order_id = payment.get("order_id")
    payment_id = payment.get("id")

    try:
        db = firestore.client()
        order_ref = _find_order_by_razorpay_id(db, rzp_order_id)
        if order_ref is None:
            print(f"[RAZORPAY WEBHOOK] {event_type}: no order for {rzp_order_id}")
        elif event_type in ("payment.captured", "order.paid"):
            result = _mark_paid_txn(db.transaction(), order_ref, payment_id)
            print(f"[RAZORPAY WEBHOOK] {event_type} {order_ref.id}: {result}")
        elif event_type == "payment.failed":
            # The customer may retry on the same Razorpay order, so the order
            # stays `pending_payment`; just record the failed attempt.
            snap = order_ref.get().to_dict() or {}
            if snap.get("payment_status") != "paid":
                order_ref.update({"payment_status": "failed"})
    except Exception as e:  # noqa: BLE001 - answer 200 only when handled; 500 makes Razorpay retry
        import traceback
        traceback.print_exc()
        return https_fn.Response("Error", status=500)

    return https_fn.Response("OK", status=200)


@firestore.transactional
def _expire_txn(transaction, order_ref):
    snap = order_ref.get(transaction=transaction)
    order = snap.to_dict() or {}
    if order.get("order_status") == "pending_payment" and order.get("payment_status") != "paid":
        transaction.update(order_ref, {
            "order_status": "payment_expired",
            "payment_status": "expired",
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        return True
    return False


@scheduler_fn.on_schedule(schedule="every 15 minutes", region=REGION)
def expire_unpaid_orders(event: scheduler_fn.ScheduledEvent) -> None:
    """Abandoned online orders (app closed / payment never finished) must not
    stay around as live orders."""
    db = firestore.client()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PAYMENT_WINDOW_MINUTES)

    # Single-field query (no composite index needed); the age is filtered here.
    pending = db.collection("orders").where("order_status", "==", "pending_payment").get()
    expired = 0
    for doc in pending:
        created = (doc.to_dict() or {}).get("created_at")
        if created is not None and created < cutoff:
            if _expire_txn(db.transaction(), doc.reference):
                expired += 1
    if expired:
        print(f"Expired {expired} unpaid online order(s)")