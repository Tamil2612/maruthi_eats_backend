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
6. on_order_refund_needed / fail_stuck_refunds — Razorpay refunds. A paid UPI
                         order that is cancelled (or that was paid after it
                         expired) is refunded automatically, in full, from the
                         server. refund.processed / refund.failed webhooks
                         keep `refund_status` up to date.

Firestore schema assumed throughout — see order_validation.py docstring.
"""

import hashlib
import hmac
import json
import os
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from firebase_admin import initialize_app, firestore, messaging, auth
from firebase_functions import firestore_fn, https_fn, scheduler_fn, options, params
import razorpay

from order_validation import (
    DELIVERY_FEE,
    MAX_LINES_PER_ORDER,
    MAX_QTY_PER_ITEM,
    effective_price,
    price_items,
    validate_coupon,
    validate_offer,
    parse_int,
    parse_float,
)
from restaurant_settings import (
    load_restaurant_settings,
    check_restaurant_availability,
    calculate_distance_km,
    validate_delivery_distance,
    calculate_delivery_fee,
    validate_minimum_order,
)

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

    # Recompute delivery fee based on stored distance and slabs
    stored_delivery_fee = parse_float(order.get("delivery_fee"), default=30.0)
    stored_distance = order.get("delivery_distance_km")

    if stored_distance is not None:
        distance_km = parse_float(stored_distance, default=0.0)
        settings = load_restaurant_settings(db)
        delivery_settings = settings.get("delivery") or {}
        expected_fee = calculate_delivery_fee(delivery_settings, distance_km)

        if abs(stored_delivery_fee - expected_fee) > 0.01:
            problems.append(
                f"Stored delivery_fee ₹{stored_delivery_fee} does not match "
                f"expected fee ₹{expected_fee} for distance {distance_km} km"
            )
        fee_for_total = Decimal(str(stored_delivery_fee))
    else:
        # Fall back to 30.0 for legacy orders without distance fields
        fee_for_total = Decimal(str(stored_delivery_fee if "delivery_fee" in order else 30.0))

    expected_total = max(Decimal('0'), recomputed_item_total - discount + fee_for_total)
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
@https_fn.on_call(region=REGION, enforce_app_check=True)
def place_order(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        data = req.data or {}
        items = data.get("items", [])
        coupon_code = data.get("coupon_code")
        if coupon_code is not None:
            if not isinstance(coupon_code, str):
                raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "Invalid coupon code")
            coupon_code = coupon_code.strip()[:40] or None

        if not isinstance(items, list) or not items:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "Cart is empty")
        if len(items) > MAX_LINES_PER_ORDER:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                f"An order can have at most {MAX_LINES_PER_ORDER} different items",
            )

        # 0. Anti-abuse rate limiting & active orders cap
        now_utc = datetime.now(timezone.utc)

        # Check real active orders count (max 3 active real orders per customer)
        active_real_orders = (
            db.collection("orders")
            .where("customer_id", "==", req.auth.uid)
            .where("order_status", "in", ["placed", "confirmed", "preparing", "out_for_delivery"])
            .get()
        )
        if len(active_real_orders) >= 3:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                "You have 3 active orders in progress. Please wait for them to be delivered before placing a new order.",
            )

        # Separate cap on unpaid pending attempts (max 5 in 30 minutes)
        cutoff_30m = now_utc - timedelta(minutes=30)
        recent_pending_orders = (
            db.collection("orders")
            .where("customer_id", "==", req.auth.uid)
            .where("order_status", "==", "pending_payment")
            .get()
        )
        unpaid_attempts_30m = 0
        for doc in recent_pending_orders:
            od_data = doc.to_dict() or {}
            created_at = od_data.get("created_at")
            if isinstance(created_at, datetime):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= cutoff_30m:
                    unpaid_attempts_30m += 1

        if unpaid_attempts_30m >= 5:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
                "Too many unpaid payment attempts. Please wait a few minutes or choose Cash on Delivery.",
            )

        # Check 60-second cooldown on real orders (ignoring pending_payment / payment_expired)
        recent_user_orders = (
            db.collection("orders")
            .where("customer_id", "==", req.auth.uid)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(10)
            .get()
        )
        for doc in recent_user_orders:
            last_order = doc.to_dict() or {}
            st = last_order.get("order_status")
            if st not in ("pending_payment", "payment_expired"):
                created_at = last_order.get("created_at")
                if isinstance(created_at, datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if (now_utc - created_at).total_seconds() < 60:
                        raise https_fn.HttpsError(
                            https_fn.FunctionsErrorCode.RESOURCE_EXHAUSTED,
                            "Please wait at least 60 seconds before placing another order.",
                        )
                break  # Newest real order checked

        # 1. Validate payment_mode strictly
        payment_mode = str(data.get("payment_mode") or "cod").lower().strip()
        if payment_mode not in ("upi", "cod"):
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                "payment_mode must be 'upi' or 'cod'",
            )

        # 2. Validate & sanitize delivery_address
        delivery_address = str(data.get("delivery_address") or "").strip()
        if not delivery_address:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                "Delivery address is required",
            )
        if len(delivery_address) > 500:
            delivery_address = delivery_address[:500]

        address_label = str(data.get("address_label") or "Home").strip()[:50]

        # 3. Validate coordinates if provided
        lat = parse_float(data["latitude"]) if "latitude" in data and data["latitude"] is not None else None
        lng = parse_float(data["longitude"]) if "longitude" in data and data["longitude"] is not None else None
        if lat is None or lng is None or (lat == 0.0 and lng == 0.0) or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                "Valid delivery coordinates (latitude and longitude) are required.",
            )

        # 4. Load restaurant settings and check availability
        settings = load_restaurant_settings(db)
        is_avail, status_code, avail_msg = check_restaurant_availability(settings, now_utc)
        if not is_avail:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                avail_msg,
            )

        # 5. Calculate delivery distance and validate delivery radius
        delivery_settings = settings.get("delivery") or {}
        rest_lat = parse_float(delivery_settings.get("restaurant_latitude"), default=0.0)
        rest_lng = parse_float(delivery_settings.get("restaurant_longitude"), default=0.0)

        if rest_lat == 0.0 and rest_lng == 0.0:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                "Restaurant location is not configured on the server.",
            )

        distance_km = calculate_distance_km(rest_lat, rest_lng, lat, lng)
        dist_valid, dist_msg = validate_delivery_distance(delivery_settings, distance_km)
        if not dist_valid:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                dist_msg,
            )

        priced_items = []
        item_total = Decimal('0')
        # BOGO bookkeeping: free units must be earned by paid units of the same offer
        bogo_offers: dict = {}
        bogo_paid_units: dict = {}
        bogo_free_units: dict = {}

        for line in items:
            if not isinstance(line, dict):
                continue

            # Enforce quantity limits on EVERY line (1 to MAX_QTY_PER_ITEM)
            qty = parse_int(line.get("qty"), default=0)
            if qty < 1 or qty > MAX_QTY_PER_ITEM:
                raise https_fn.HttpsError(
                    https_fn.FunctionsErrorCode.INVALID_ARGUMENT,
                    f"Quantity for each item must be between 1 and {MAX_QTY_PER_ITEM}. Received: {qty}",
                )

            is_offer = bool(line.get("is_offer"))
            is_free = bool(line.get("is_free"))
            offer_id = str(line.get("offer_id") or "")
            parent_offer_id = str(line.get("parent_offer_id") or "")

            # --- CASE A: Offer lines or Free items ---
            if is_offer or is_free or offer_id or parent_offer_id:
                target_offer_id = offer_id or parent_offer_id
                if not target_offer_id:
                    raise https_fn.HttpsError(
                        https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                        "Offer line missing offer_id",
                    )

                offer, offer_err = validate_offer(db, target_offer_id)
                if offer_err:
                    raise https_fn.HttpsError(
                        https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                        offer_err,
                    )

                offer_type = offer.get("type", "combo")

                if is_free:
                    # Free item MUST be part of a valid active BOGO offer
                    if offer_type != "bogo":
                        raise https_fn.HttpsError(
                            https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                            "Item marked free is not part of a BOGO offer",
                        )
                    bogo_offers[target_offer_id] = offer
                    bogo_free_units[target_offer_id] = bogo_free_units.get(target_offer_id, 0) + qty
                    priced_items.append({
                        "item_id": str(offer.get("get_item_id") or line.get("item_id") or f"{target_offer_id}_get"),
                        "name": str(offer.get("get_item_name") or "Free Item"),
                        "price": 0.0,
                        "qty": qty,
                        "is_offer": True,
                        "is_free": True,
                        "offer_id": target_offer_id,
                        "offer_description": f"FREE with {offer.get('title') or 'offer'}",
                    })
                    continue

                if offer_type == "combo":
                    # Server authoritative price for combo offer
                    combo_price = parse_float(offer.get("combo_price", 0))
                    item_total += Decimal(str(combo_price)) * qty
                    priced_items.append({
                        "item_id": f"offer_{target_offer_id}",
                        "name": str(offer.get("title") or "Combo Offer"),
                        "price": combo_price,
                        "qty": qty,
                        "is_offer": True,
                        "is_free": False,
                        "offer_id": target_offer_id,
                        "offer_description": str(offer.get("description") or ""),
                        "is_combo": True,
                        "bundle_items": offer.get("bundle_items") or [],
                    })
                    continue

                if offer_type == "bogo":
                    buy_item_id = str(offer.get("buy_item_id") or "")
                    menu_doc = db.collection("menu_items").document(buy_item_id).get() if buy_item_id else None
                    if not menu_doc or not menu_doc.exists:
                        raise https_fn.HttpsError(
                            https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                            f"The offer '{offer.get('title', target_offer_id)}' is no longer available",
                        )
                    menu_item = menu_doc.to_dict() or {}
                    if not menu_item.get("available", True):
                        raise https_fn.HttpsError(
                            https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                            f"{menu_item.get('name', 'Item')} is currently unavailable",
                        )
                    real_price = float(effective_price(menu_item))

                    bogo_offers[target_offer_id] = offer
                    bogo_paid_units[target_offer_id] = bogo_paid_units.get(target_offer_id, 0) + qty
                    item_total += Decimal(str(real_price)) * qty
                    priced_items.append({
                        "item_id": buy_item_id,
                        "name": str(offer.get("buy_item_name") or menu_item.get("name") or "Item"),
                        "price": real_price,
                        "qty": qty,
                        "is_offer": True,
                        "is_free": False,
                        "offer_id": target_offer_id,
                        "offer_description": f"Part of: {offer.get('title') or 'offer'}",
                    })
                    continue

            # --- CASE B: Regular Menu Items ---
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

            real_price = float(effective_price(menu_item))
            item_total += Decimal(str(real_price)) * qty
            priced_items.append({
                "item_id": str(item_id),
                "name": str(menu_item.get("name", "")),
                "price": real_price,
                "qty": qty,
                "is_offer": False,
                "is_free": False,
            })

        # Free units must be earned: floor(paid / buy_qty) * get_qty per BOGO offer.
        for offer_key, free_units in bogo_free_units.items():
            offer = bogo_offers[offer_key]
            buy_qty = max(1, parse_int(offer.get("buy_qty"), default=1))
            get_qty = max(1, parse_int(offer.get("get_qty"), default=1))
            earned = (bogo_paid_units.get(offer_key, 0) // buy_qty) * get_qty
            if free_units > earned:
                raise https_fn.HttpsError(
                    https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                    f"Free items in '{offer.get('title', offer_key)}' do not match the items bought",
                )

        item_total_dec = item_total.quantize(Decimal('0.01'))
        food_subtotal = float(item_total_dec)

        # 6. Validate Minimum Order Value against Food Subtotal (before coupon, excluding delivery)
        min_valid, min_msg = validate_minimum_order(settings, food_subtotal)
        if not min_valid:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                min_msg,
            )

        # 7. Validate Coupon
        discount_dec, coupon_error = validate_coupon(db, coupon_code, item_total_dec)
        if coupon_error:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, coupon_error)

        discount_val = float(discount_dec)

        # 8. Calculate Authoritative Delivery Fee based on distance slabs
        delivery_fee = calculate_delivery_fee(delivery_settings, distance_km)

        # 9. Calculate Final Total
        total_val = round(max(0.0, food_subtotal - discount_val + delivery_fee), 2)

        order_ref = db.collection("orders").document()
        order_ref.set({
            "customer_id": req.auth.uid,
            "items": priced_items,
            "item_total": food_subtotal,
            "delivery_fee": delivery_fee,
            "delivery_distance_km": distance_km,
            "minimum_order_value_at_order": parse_float(settings.get("minimum_order_value"), default=150.0),
            "restaurant_timezone": str(settings.get("timezone", "Asia/Kolkata")),
            "coupon_code": coupon_code,
            "coupon_discount": discount_val,
            "total": total_val,
            "payment_mode": payment_mode,
            "payment_status": "pending" if payment_mode == "upi" else "cod_pending",
            "order_status": "pending_payment" if payment_mode == "upi" else "placed",
            "delivery_address": delivery_address,
            "address_label": address_label,
            "latitude": lat,
            "longitude": lng,
            "validation_status": "ok",
            "created_at": firestore.SERVER_TIMESTAMP,
        })

        return {"order_id": order_ref.id, "total": total_val, "delivery_fee": delivery_fee, "distance_km": distance_km}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] place_order ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION)
def get_checkout_preview(req: https_fn.CallableRequest) -> dict:
    """Calculates server-authoritative delivery distance, delivery fee,
    minimum order status, and restaurant availability for the Flutter checkout screen."""
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        data = req.data or {}
        lat = parse_float(data.get("latitude"))
        lng = parse_float(data.get("longitude"))
        subtotal = parse_float(data.get("subtotal"))

        db = firestore.client()
        settings = load_restaurant_settings(db)
        is_avail, status_code, status_msg = check_restaurant_availability(settings)

        delivery = settings.get("delivery") or {}
        rest_lat = parse_float(delivery.get("restaurant_latitude"))
        rest_lng = parse_float(delivery.get("restaurant_longitude"))

        distance_km = 0.0
        delivery_fee = 30.0
        is_deliverable = True
        delivery_msg = ""

        if lat != 0.0 and lng != 0.0 and rest_lat != 0.0 and rest_lng != 0.0:
            distance_km = calculate_distance_km(rest_lat, rest_lng, lat, lng)
            is_deliverable, delivery_msg = validate_delivery_distance(delivery, distance_km)
            delivery_fee = calculate_delivery_fee(delivery, distance_km)

        min_valid, min_msg = validate_minimum_order(settings, subtotal)

        return {
            "is_available": is_avail,
            "status_code": status_code,
            "status_message": status_msg,
            "distance_km": distance_km,
            "delivery_fee": delivery_fee,
            "is_deliverable": is_deliverable,
            "delivery_message": delivery_msg,
            "minimum_order_value": parse_float(settings.get("minimum_order_value"), default=150.0),
            "meets_minimum_order": min_valid,
            "minimum_order_message": min_msg,
        }
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] get_checkout_preview ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
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


@https_fn.on_call(region=REGION, enforce_app_check=True, secrets=[RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET])
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
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] razorpay_create_order ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION, enforce_app_check=True, secrets=[RAZORPAY_KEY_SECRET])
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

        client, key_id = _razorpay_client()
        try:
            payment = client.payment.fetch(payment_id)
        except Exception as p_err:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                f"Could not fetch payment details from Razorpay: {p_err}",
            )

        stored_rzp_order_id = order.get("razorpay_order_id") or rzp_order_id
        p_order_id = payment.get("order_id")
        if p_order_id and p_order_id != stored_rzp_order_id:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.PERMISSION_DENIED,
                "Payment does not belong to this Razorpay order",
            )

        stored_amount_paise = order.get("razorpay_amount")
        if stored_amount_paise is None:
            stored_amount_paise = int((Decimal(str(parse_float(order.get("total", 0)))) * 100).to_integral_value())

        p_amount = parse_int(payment.get("amount"))
        if p_amount != stored_amount_paise:
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                f"Payment amount ({p_amount}) does not match expected order amount ({stored_amount_paise})",
            )

        if payment.get("currency") != "INR":
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                "Invalid payment currency",
            )

        if payment.get("status") != "captured":
            raise https_fn.HttpsError(
                https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
                f"Payment status is '{payment.get('status')}', not captured",
            )

        status = _mark_paid_txn(db.transaction(), order_ref, payment_id)
        return {"status": status}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] razorpay_verify_payment ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
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
    entities = payload.get("payload", {})
    payment = entities.get("payment", {}).get("entity", {})
    rzp_order_id = payment.get("order_id")
    payment_id = payment.get("id")

    try:
        db = firestore.client()

        # Refund results (see section 6). They carry a refund AND a payment entity.
        if event_type in ("refund.processed", "refund.failed"):
            _handle_refund_webhook(db, event_type, entities.get("refund", {}).get("entity", {}), payment_id)
            return https_fn.Response("OK", status=200)

        order_ref = _find_order_by_razorpay_id(db, rzp_order_id)
        if order_ref is None:
            print(f"[RAZORPAY WEBHOOK] {event_type}: no order for {rzp_order_id}")
        elif event_type in ("payment.captured", "order.paid"):
            snap = order_ref.get().to_dict() or {}
            stored_amount_paise = snap.get("razorpay_amount")
            if stored_amount_paise is None:
                stored_amount_paise = int((Decimal(str(parse_float(snap.get("total", 0)))) * 100).to_integral_value())

            p_amount = parse_int(payment.get("amount"))
            p_currency = payment.get("currency")
            p_status = payment.get("status")

            if (p_currency == "INR" and
                p_status in ("captured", "authorized") and
                (p_amount == stored_amount_paise or p_amount == 0)):
                result = _mark_paid_txn(db.transaction(), order_ref, payment_id)
                print(f"[RAZORPAY WEBHOOK] {event_type} {order_ref.id}: {result}")
            else:
                print(f"[RAZORPAY WEBHOOK REJECTED] {event_type} {order_ref.id}: amount={p_amount} vs {stored_amount_paise}, status={p_status}")
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


# =======================================================================
# 6. Refunds — Razorpay (UPI)
#
# Order fields written here (server only):
#   refund_status      pending | processed | failed | retry_requested
#   refund_reason      order_cancelled | late_payment
#   refund_amount      rupees
#   refund_id          Razorpay refund id (rfnd_...)
#   refund_requested_at / refunded_at / refund_error
#
# When a refund starts:
#   * order_status becomes "cancelled" and the UPI payment was captured
#   * payment_status becomes "paid_needs_refund" (paid after the order expired)
#   * the admin app sets refund_status to "retry_requested" (Retry button)
# Cash-on-delivery orders and unpaid orders are never refunded.
# =======================================================================
def _rupees(paise) -> float:
    return float((Decimal(int(paise or 0)) / 100).quantize(Decimal("0.01")))


def _fmt_rupees(paise) -> str:
    value = _rupees(paise)
    return f"{value:.0f}" if value == int(value) else f"{value:.2f}"


def _find_order_by_payment_id(db, payment_id):
    if not payment_id:
        return None
    docs = (
        db.collection("orders")
        .where("razorpay_payment_id", "==", payment_id)
        .limit(1)
        .get()
    )
    return docs[0].reference if docs else None


def _send_customer_push(customer_id, body: str, data: dict) -> None:
    """Best-effort push to the customer's phone (same channel as order updates)."""
    if not customer_id:
        return
    try:
        db = firestore.client()
        user_doc = db.collection("users").document(customer_id).get()
        fcm_token = (user_doc.to_dict() or {}).get("fcm_token") if user_doc.exists else None
        if not fcm_token:
            return
        messaging.send(messaging.Message(
            notification=messaging.Notification(title="Maruthi Eats", body=body),
            data={k: str(v) for k, v in data.items()},
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="order_status_channel",
                    priority="high",
                    default_sound=True,
                ),
            ),
            token=fcm_token,
        ))
    except Exception as e:  # noqa: BLE001 - a bad token must never break a refund
        print(f"Failed to send refund notification to {customer_id}: {e}")


@firestore.transactional
def _claim_refund_txn(transaction, order_ref, reason):
    """Decides - atomically - whether THIS call may start a refund.

    Returns the Razorpay payment id to refund, or None. Because the status is
    flipped to `pending` inside the transaction, a re-delivered trigger or a
    double tap on Retry can never refund twice."""
    order = order_ref.get(transaction=transaction).to_dict() or {}

    if order.get("payment_mode") != "upi":
        return None
    if order.get("refund_status") in ("pending", "processed"):
        return None
    if order.get("payment_status") not in ("paid", "paid_needs_refund"):
        return None  # nothing was paid, nothing to give back

    payment_id = order.get("razorpay_payment_id")
    if not payment_id:
        transaction.update(order_ref, {
            "refund_status": "failed",
            "refund_reason": reason,
            "refund_error": "No Razorpay payment id on this order - refund it from the Razorpay dashboard.",
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        _notify_admin_refund_failed(order_ref.id)
        return None

    transaction.update(order_ref, {
        "refund_status": "pending",
        "refund_reason": reason,
        "refund_requested_at": firestore.SERVER_TIMESTAMP,
        "refund_error": firestore.DELETE_FIELD,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    return payment_id


@firestore.transactional
def _record_refund_created_txn(transaction, order_ref, refund_id, amount_paise):
    """Razorpay accepted the refund but it is not finished yet. Never moves a
    refund backwards: the refund.processed webhook may already have arrived."""
    order = order_ref.get(transaction=transaction).to_dict() or {}
    if order.get("refund_status") == "processed":
        return
    transaction.update(order_ref, {
        "refund_status": "pending",
        "refund_id": refund_id,
        "refund_amount": _rupees(amount_paise),
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


@firestore.transactional
def _finish_refund_txn(transaction, order_ref, refund_id, amount_paise):
    """Marks the refund processed. True only for the call that makes the change."""
    order = order_ref.get(transaction=transaction).to_dict() or {}
    if order.get("refund_status") == "processed":
        return False
    amount = amount_paise or order.get("razorpay_amount") or 0
    update = {
        "refund_status": "processed",
        "refund_amount": _rupees(amount),
        "refunded_at": firestore.SERVER_TIMESTAMP,
        "refund_error": firestore.DELETE_FIELD,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if refund_id:
        update["refund_id"] = refund_id
    transaction.update(order_ref, update)
    return True


def _finish_refund(db, order_ref, refund_id, amount_paise) -> None:
    if _finish_refund_txn(db.transaction(), order_ref, refund_id, amount_paise):
        order = order_ref.get().to_dict() or {}
        amount = amount_paise or order.get("razorpay_amount") or 0
        _send_customer_push(
            order.get("customer_id"),
            f"Your refund of ₹{_fmt_rupees(amount)} has been processed",
            {"order_id": order_ref.id, "type": "refund"},
        )


def _notify_admin_refund_failed(order_id: str) -> None:
    """Normal (visible) notification for the restaurant staff. It deliberately has
    no `order_id` key, so the admin app does NOT treat it as a new order."""
    try:
        messaging.send(messaging.Message(
            topic="admin_orders",
            notification=messaging.Notification(
                title="Refund failed",
                body=f"Order #{order_id[:6].upper()}: the refund did not go through. "
                     "Open the order and press Retry.",
            ),
            data={"type": "refund_failed", "refund_order_id": order_id},
            android=messaging.AndroidConfig(priority="high"),
        ))
    except Exception as e:  # noqa: BLE001
        print(f"Failed to send refund-failed alert for {order_id}: {e}")


def _fail_refund(order_ref, message: str) -> None:
    order_ref.update({
        "refund_status": "failed",
        "refund_error": message[:200],
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
    _notify_admin_refund_failed(order_ref.id)


def _run_refund(db, order_ref, payment_id: str, reason: str) -> None:
    """Talks to Razorpay. Only ever called after _claim_refund_txn said yes."""
    try:
        client, _ = _razorpay_client()
        payment = client.payment.fetch(payment_id)

        if payment.get("status") not in ("captured", "refunded"):
            raise RuntimeError(f"payment is '{payment.get('status')}', not captured")

        # Refund exactly what is still refundable. This also makes a retry safe
        # when an earlier attempt reached Razorpay but its reply was lost.
        remaining = int(payment.get("amount", 0)) - int(payment.get("amount_refunded") or 0)
        if remaining <= 0:
            _finish_refund(db, order_ref, None, int(payment.get("amount_refunded") or 0))
            return

        refund = client.payment.refund(payment_id, {
            "amount": remaining,
            "speed": "optimum",  # instant when Razorpay can, otherwise normal (5-7 working days)
            "receipt": order_ref.id,
            "notes": {"firestore_order_id": order_ref.id, "reason": reason},
        })

        if refund.get("status") == "processed":
            _finish_refund(db, order_ref, refund.get("id"), remaining)
        else:
            _record_refund_created_txn(db.transaction(), order_ref, refund.get("id"), remaining)
    except Exception as e:  # noqa: BLE001 - surfaced to the admin as a failed refund
        import traceback
        traceback.print_exc()
        text = str(e)
        if isinstance(e, https_fn.HttpsError):
            text = e.message
        _fail_refund(order_ref, f"{type(e).__name__}: {text}")


@firestore_fn.on_document_updated(
    document="orders/{order_id}",
    region=REGION,
    secrets=[RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET],
)
def on_order_refund_needed(event: firestore_fn.Event) -> None:
    before = event.data.before.to_dict() or {}
    after = event.data.after.to_dict() or {}

    reason = None
    if before.get("order_status") != "cancelled" and after.get("order_status") == "cancelled":
        reason = "order_cancelled"
    elif before.get("payment_status") != "paid_needs_refund" and after.get("payment_status") == "paid_needs_refund":
        reason = "late_payment"
    elif before.get("refund_status") != "retry_requested" and after.get("refund_status") == "retry_requested":
        reason = after.get("refund_reason") or "order_cancelled"

    if reason is None or after.get("payment_mode") != "upi":
        return

    db = firestore.client()
    order_ref = event.data.after.reference
    payment_id = _claim_refund_txn(db.transaction(), order_ref, reason)
    if payment_id:
        print(f"[REFUND] {order_ref.id}: starting ({reason})")
        _run_refund(db, order_ref, payment_id, reason)


def _handle_refund_webhook(db, event_type, refund, payment_id) -> None:
    pid = refund.get("payment_id") or payment_id
    order_ref = _find_order_by_payment_id(db, pid)
    if order_ref is None:
        print(f"[RAZORPAY WEBHOOK] {event_type}: no order for payment {pid}")
        return

    if event_type == "refund.processed":
        _finish_refund(db, order_ref, refund.get("id"), int(refund.get("amount") or 0))
    else:  # refund.failed
        current = order_ref.get().to_dict() or {}
        if current.get("refund_status") != "processed":
            _fail_refund(
                order_ref,
                "The refund failed at the bank. Press Retry, or refund it from the Razorpay dashboard.",
            )
    print(f"[RAZORPAY WEBHOOK] {event_type} {order_ref.id}")


@firestore.transactional
def _fail_stuck_txn(transaction, order_ref):
    order = order_ref.get(transaction=transaction).to_dict() or {}
    if order.get("refund_status") == "pending" and not order.get("refund_id"):
        transaction.update(order_ref, {
            "refund_status": "failed",
            "refund_error": "The refund did not complete. Press Retry.",
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        return True
    return False


@scheduler_fn.on_schedule(schedule="every 30 minutes", region=REGION)
def fail_stuck_refunds(event: scheduler_fn.ScheduledEvent) -> None:
    """A refund that was claimed but never reached Razorpay (no refund_id after
    15 minutes, e.g. the function crashed) is shown as failed so the admin can
    retry it. A refund that Razorpay accepted can legitimately stay `pending`
    for days, so those are left alone."""
    db = firestore.client()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    for doc in db.collection("orders").where("refund_status", "==", "pending").get():
        data = doc.to_dict() or {}
        requested = data.get("refund_requested_at")
        if not data.get("refund_id") and requested is not None and requested < cutoff:
            if _fail_stuck_txn(db.transaction(), doc.reference):
                print(f"[REFUND] {doc.id}: marked failed (never reached Razorpay)")
                _notify_admin_refund_failed(doc.id)


@https_fn.on_call(region=REGION)
def delete_account(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    uid = req.auth.uid
    db = firestore.client()

    # 1. Check for active orders or pending refunds before deleting anything
    active_orders = (
        db.collection("orders")
        .where("customer_id", "==", uid)
        .where("order_status", "in", ["placed", "confirmed", "preparing", "out_for_delivery", "pending_payment"])
        .get()
    )
    pending_refunds = (
        db.collection("orders")
        .where("customer_id", "==", uid)
        .where("refund_status", "==", "pending")
        .get()
    )

    if active_orders or pending_refunds:
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            "You have an order or refund in progress. Please wait for it to complete before deleting your account.",
        )

    try:
        # 2. Delete user subcollections (e.g. addresses) using batch
        batch = db.batch()
        batch_count = 0

        addresses_ref = db.collection("users").document(uid).collection("addresses").get()
        for doc in addresses_ref:
            batch.delete(doc.reference)
            batch_count += 1
            if batch_count >= 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0

        # 3. Delete user profile doc
        user_doc_ref = db.collection("users").document(uid)
        if user_doc_ref.get().exists:
            batch.delete(user_doc_ref)
            batch_count += 1

        # 4. Anonymize historical orders (PII removed, preserving financial/sales records)
        orders_ref = db.collection("orders").where("customer_id", "==", uid).get()
        anonymized_id = f"anonymized_{uid[:8]}"

        for order_doc in orders_ref:
            batch.update(order_doc.reference, {
                "customer_id": anonymized_id,
                "delivery_address": "Anonymized Address",
                "address_label": "Anonymized",
                "latitude": None,
                "longitude": None,
            })
            batch_count += 1
            if batch_count >= 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0

        if batch_count > 0:
            batch.commit()

        # 5. Delete Firebase Auth user LAST (idempotent / safe to retry)
        try:
            auth.delete_user(uid)
        except Exception as auth_err:
            if "NOT_FOUND" not in str(auth_err) and "UserNotFoundError" not in type(auth_err).__name__:
                print(f"[DELETE ACCOUNT] Auth delete notice: {auth_err}")

        return {"status": "success", "message": "Account successfully deleted."}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] delete_account ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


def _require_staff(db, uid: str) -> None:
    staff_doc = db.collection("staff").document(uid).get()
    if not staff_doc.exists:
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.PERMISSION_DENIED,
            "Access denied: Admin/staff privileges required.",
        )


@https_fn.on_call(region=REGION)
def update_order_status(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        _require_staff(db, req.auth.uid)

        data = req.data or {}
        order_id = str(data.get("order_id") or "").strip()
        new_status = str(data.get("order_status") or "").strip().lower()

        valid_statuses = ["placed", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled"]
        if not order_id or new_status not in valid_statuses:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, f"Invalid order_id or order_status: {new_status}")

        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        # Update ONLY allowed fields
        order_ref.update({
            "order_status": new_status,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "updated_by": req.auth.uid,
            "status_changed_by": req.auth.uid,
        })

        # Append to status_log subcollection
        order_ref.collection("status_log").add({
            "status": new_status,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "changed_by": req.auth.uid,
        })

        return {"order_id": order_id, "order_status": new_status}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] update_order_status ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION)
def mark_cod_collected(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        _require_staff(db, req.auth.uid)

        data = req.data or {}
        order_id = str(data.get("order_id") or "").strip()

        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        if order.get("payment_mode") != "cod":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, "Order is not a Cash on Delivery order")

        # Update ONLY allowed fields
        order_ref.update({
            "payment_status": "paid",
            "updated_at": firestore.SERVER_TIMESTAMP,
            "updated_by": req.auth.uid,
        })

        return {"order_id": order_id, "payment_status": "paid"}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] mark_cod_collected ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION)
def cancel_order(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        data = req.data or {}
        order_id = str(data.get("order_id") or "").strip()
        reason = str(data.get("reason") or "Order cancelled").strip()[:200]

        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        is_staff = db.collection("staff").document(req.auth.uid).get().exists
        is_owner = order.get("customer_id") == req.auth.uid

        if not is_staff and not is_owner:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED, "Permission denied")

        if not is_staff and order.get("order_status") != "placed":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, "Orders in progress cannot be cancelled by customer")

        # Update ONLY allowed fields
        order_ref.update({
            "order_status": "cancelled",
            "cancel_reason": reason,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "updated_by": req.auth.uid,
        })

        order_ref.collection("status_log").add({
            "status": "cancelled",
            "reason": reason,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "changed_by": req.auth.uid,
        })

        return {"order_id": order_id, "order_status": "cancelled"}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] cancel_order ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION)
def retry_refund(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        db = firestore.client()
        _require_staff(db, req.auth.uid)

        data = req.data or {}
        order_id = str(data.get("order_id") or "").strip()

        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()
        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        if order.get("refund_status") != "failed":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, "Only failed refunds can be retried")

        order_ref.update({
            "refund_status": "retry_requested",
            "updated_at": firestore.SERVER_TIMESTAMP,
            "updated_by": req.auth.uid,
        })

        return {"order_id": order_id, "refund_status": "retry_requested"}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] retry_refund ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )


@https_fn.on_call(region=REGION)
def submit_order_rating(req: https_fn.CallableRequest) -> dict:
    if req.auth is None:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Login required")

    try:
        data = req.data or {}
        order_id = str(data.get("order_id") or "").strip()
        rating = parse_int(data.get("rating"), default=0)

        if not order_id or rating < 1 or rating > 5:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INVALID_ARGUMENT, "Valid order_id and rating (1-5) required")

        db = firestore.client()
        order_ref = db.collection("orders").document(order_id)
        order_doc = order_ref.get()

        if not order_doc.exists:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.NOT_FOUND, "Order not found")

        order = order_doc.to_dict() or {}
        if order.get("customer_id") != req.auth.uid:
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED, "Not your order")

        if order.get("order_status") != "delivered":
            raise https_fn.HttpsError(https_fn.FunctionsErrorCode.FAILED_PRECONDITION, "Only delivered orders can be rated")

        order_ref.update({
            "rating": rating,
            "rating_submitted_at": firestore.SERVER_TIMESTAMP,
        })

        return {"status": "success", "rating": rating}
    except https_fn.HttpsError:
        raise
    except Exception as e:
        ref_id = _generate_ref_id()
        print(f"[INTERNAL ERROR] submit_order_rating ref={ref_id}: {type(e).__name__} - {e}")
        traceback.print_exc()
        raise https_fn.HttpsError(
            https_fn.FunctionsErrorCode.INTERNAL,
            f"Something went wrong. Please try again. (ref {ref_id})",
        )