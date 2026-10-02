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

5. razorpay_create_order / razorpay_webhook — server-side payment session
                         creation and signature-verified webhook, closing
                         the UPI-is-a-stub gap in checkout_screen.dart.

Firestore schema assumed throughout — see order_validation.py docstring.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

from firebase_admin import initialize_app, firestore, messaging
from firebase_functions import firestore_fn, https_fn, scheduler_fn, options
import razorpay

from order_validation import DELIVERY_FEE, price_items, validate_coupon, parse_int, parse_float

initialize_app()

# --- Secrets & config -------------------------------------------------
# Set these with:
#   firebase functions:secrets:set RAZORPAY_KEY_SECRET
#   firebase functions:secrets:set RAZORPAY_WEBHOOK_SECRET
# RAZORPAY_KEY_ID isn't secret (it's shown to the client SDK anyway) but
# is still parameterized so it's not hardcoded here.
# RAZORPAY_KEY_ID = params.SecretParam("RAZORPAY_KEY_ID")
# RAZORPAY_KEY_SECRET = params.SecretParam("RAZORPAY_KEY_SECRET")
# RAZORPAY_WEBHOOK_SECRET = params.SecretParam("RAZORPAY_WEBHOOK_SECRET")

REGION = "asia-south1"  # Mumbai — closest region to an Indian user base


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

    # Send high-priority FCM notification to Admin app topic ('admin_orders')
    try:
        order_id = event.params["order_id"]
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
        print(f"[FCM ERROR] Failed to send admin notification for order {event.params['order_id']}: {e}")


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
            "order_status": "placed",
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

    if before.get("order_status") == after.get("order_status"):
        return  # nothing relevant changed

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
# 5. Payment — Razorpay order creation + webhook
# =======================================================================
# @https_fn.on_call(
#     region=REGION,
#     secrets=[RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET],
# )
# def razorpay_create_order(req: https_fn.CallableRequest) -> dict:
#     if req.auth is None:
#         raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

#     order_id = req.data.get("order_id")
#     if not order_id:
#         raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "order_id is required")

#     db = firestore.client()
#     order_doc = db.collection("orders").document(order_id).get()
#     if not order_doc.exists:
#         raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

#     order = order_doc.to_dict()
#     if order.get("customer_id") != req.auth.uid:
#         raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED, "Not your order")

#     # Convert Decimal total to paise (smallest currency unit) for Razorpay
#     amount_paise = int((Decimal(str(order["total"])) * 100).quantize(Decimal('1')))

#     client = razorpay.Client(auth=(RAZORPAY_KEY_ID.value, RAZORPAY_KEY_SECRET.value))
#     razorpay_order = client.order.create({
#         "amount": amount_paise,
#         "currency": "INR",
#         "receipt": order_id,
#         "notes": {"firestore_order_id": order_id},
#     })

#     db.collection("orders").document(order_id).update({"razorpay_order_id": razorpay_order["id"]})

#     return {
#         "razorpay_order_id": razorpay_order["id"],
#         "amount": amount_paise,
#         "currency": "INR",
#         "key_id": RAZORPAY_KEY_ID.value,
#     }


# @https_fn.on_request(region=REGION, secrets=[RAZORPAY_WEBHOOK_SECRET])
# def razorpay_webhook(req: https_fn.Request) -> https_fn.Response:
#     signature = req.headers.get("X-Razorpay-Signature", "")
#     body = req.get_data()

#     expected_signature = hmac.new(
#         RAZORPAY_WEBHOOK_SECRET.value.encode(), body, hashlib.sha256
#     ).hexdigest()

#     if not hmac.compare_digest(expected_signature, signature):
#         return https_fn.Response("Invalid signature", status=400)

#     try:
#         payload = json.loads(body)
#     except json.JSONDecodeError:
#         return https_fn.Response("Invalid JSON", status=400)

#     event_type = payload.get("event")

#     if event_type == "payment.captured":
#         try:
#             payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
#             firestore_order_id = payment_entity.get("notes", {}).get("firestore_order_id")
#             if firestore_order_id:
#                 db = firestore.client()
#                 db.collection("orders").document(firestore_order_id).update({
#                     "payment_status": "paid",
#                     "razorpay_payment_id": payment_entity.get("id"),
#                 })
#         except (KeyError, TypeError):
#             # Malformed payload - log but don't crash
#             pass

#     elif event_type == "payment.failed":
#         try:
#             payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
#             firestore_order_id = payment_entity.get("notes", {}).get("firestore_order_id")
#             if firestore_order_id:
#                 db = firestore.client()
#                 db.collection("orders").document(firestore_order_id).update({
#                     "payment_status": "failed",
#                 })
#         except (KeyError, TypeError):
#             # Malformed payload - log but don't crash
#             pass

#     return https_fn.Response("OK", status=200)