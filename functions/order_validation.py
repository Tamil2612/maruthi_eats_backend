"""
Shared order-pricing and coupon-validation logic.

This mirrors the pricing rules currently implemented client-side in the
Flutter app (lib/providers/cart_provider.dart and lib/screens/checkout_screen.dart),
so it can be used to either:
  1. Audit an order after the client creates it (see main.py: on_order_created), or
  2. Compute the authoritative price when the server creates the order itself
     (see main.py: place_order).

Firestore schema this assumes (matches the existing app exactly):
  menu_items/{id}: name, description, price, category, image_url,
                    is_veg, available, discount_price, has_discount
  coupons/{id}:     code, amount, min_order_value, expiry_date, rules, is_active
"""

from datetime import datetime, timezone
from google.cloud.firestore import Client as FirestoreClient

# Flat delivery fee — matches CartProvider.deliveryFee in the Flutter app
# (lib/providers/cart_provider.dart: `isEmpty ? 0.0 : 30.0`).
# If you ever make this dynamic (distance-based, surge, etc.), this is the
# one place to change it server-side.
DELIVERY_FEE = 30.0


def effective_price(menu_item: dict) -> float:
    """The real, current price of a menu item, honoring an active discount."""
    if menu_item.get("has_discount"):
        return float(menu_item.get("discount_price") or 0)
    return float(menu_item.get("price") or 0)


def price_items(db: FirestoreClient, items: list[dict]) -> tuple[float, list[str]]:
    """
    Given a list of order line items (the same shape CartItem.toOrderMap()
    produces: item_id, name, price, qty, is_offer, is_free, ...), look up
    each item's REAL current price from Firestore and recompute the total.

    Returns (recomputed_item_total, list_of_problems). An empty problems
    list means everything checked out. Items marked `is_free` are expected
    to be priced at 0 (BOGO/offer freebies) — anything else is flagged.
    Items marked `is_offer` but not free are trusted at their submitted
    price for now, since combo/bundle pricing is set by the admin per-offer
    rather than derived from individual item prices; flag this as a known
    gap if you want tighter validation on combo pricing specifically.
    """
    problems: list[str] = []
    recomputed_total = 0.0

    for line in items:
        item_id = line.get("item_id")
        qty = int(line.get("qty") or 0)
        submitted_price = float(line.get("price") or 0)
        is_free = bool(line.get("is_free"))
        is_offer = bool(line.get("is_offer"))

        if is_free:
            if submitted_price != 0:
                problems.append(f"Item {item_id} marked free but priced at {submitted_price}")
            continue  # free items don't contribute to the total

        if is_offer:
            # Trusted as-is — see docstring. Still counts toward the total.
            recomputed_total += submitted_price * qty
            continue

        menu_doc = db.collection("menu_items").document(item_id).get()
        if not menu_doc.exists:
            problems.append(f"Item {item_id} no longer exists in the menu")
            continue

        menu_item = menu_doc.to_dict()
        if not menu_item.get("available", True):
            problems.append(f"Item {item_id} ({menu_item.get('name')}) is not currently available")

        real_price = effective_price(menu_item)
        if abs(real_price - submitted_price) > 0.01:
            problems.append(
                f"Item {item_id} ({menu_item.get('name')}) priced at {submitted_price}, "
                f"but current price is {real_price}"
            )

        recomputed_total += real_price * qty

    return round(recomputed_total, 2), problems


def validate_coupon(db: FirestoreClient, coupon_code: str | None, item_total: float) -> tuple[float, str | None]:
    """
    Re-checks a coupon the same way the Flutter admin/customer apps do
    (lib/screens/coupons_screen.dart, lib/providers/cart_provider.dart):
    must exist, be active, not expired, and item_total must meet its
    minimum order value.

    Returns (discount_amount, error_message). error_message is None if the
    coupon is valid (or none was applied); discount_amount is 0 if invalid.
    """
    if not coupon_code:
        return 0.0, None

    matches = db.collection("coupons").where("code", "==", coupon_code).limit(1).get()
    if not matches:
        return 0.0, f"Coupon '{coupon_code}' does not exist"

    coupon = matches[0].to_dict()

    if not coupon.get("is_active", True):
        return 0.0, f"Coupon '{coupon_code}' is not active"

    expiry = coupon.get("expiry_date")
    if expiry is not None:
        # Firestore Timestamps come back as timezone-aware datetimes already.
        if expiry < datetime.now(timezone.utc):
            return 0.0, f"Coupon '{coupon_code}' has expired"

    min_order = float(coupon.get("min_order_value") or 0)
    if item_total < min_order:
        return 0.0, f"Coupon '{coupon_code}' requires a minimum order of {min_order}, cart is {item_total}"

    return float(coupon.get("amount") or 0), None
