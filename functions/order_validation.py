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
from decimal import Decimal
from google.cloud.firestore import Client as FirestoreClient

# Flat delivery fee — matches CartProvider.deliveryFee in the Flutter app
# (lib/providers/cart_provider.dart: `isEmpty ? 0.0 : 30.0`).
# If you ever make this dynamic (distance-based, surge, etc.), this is the
# one place to change it server-side.
DELIVERY_FEE = 30.0


def parse_int(val, default=1) -> int:
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str):
        try:
            return int(float(val))
        except ValueError:
            return default
    if isinstance(val, dict):
        for k in ("qty", "quantity", "value", "val", "count", "number"):
            if k in val:
                return parse_int(val[k], default)
        for v in val.values():
            if isinstance(v, (int, float, str)):
                return parse_int(v, default)
    return default


def parse_float(val, default=0.0) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return default
    if isinstance(val, dict):
        for k in ("price", "amount", "value", "val"):
            if k in val:
                return parse_float(val[k], default)
        for v in val.values():
            if isinstance(v, (int, float, str)):
                return parse_float(v, default)
    return default


def effective_price(menu_item: dict) -> Decimal:
    """The real, current price of a menu item, honoring an active discount."""
    if menu_item.get("has_discount"):
        return Decimal(str(parse_float(menu_item.get("discount_price"))))
    return Decimal(str(parse_float(menu_item.get("price"))))


def price_items(db: FirestoreClient, items: list[dict]) -> tuple[Decimal, list[str]]:
    problems: list[str] = []
    recomputed_total = Decimal('0')

    for line in items:
        if not isinstance(line, dict):
            continue
        item_id = line.get("item_id")
        if isinstance(item_id, dict):
            item_id = item_id.get("id") or item_id.get("item_id") or str(item_id)

        qty = parse_int(line.get("qty"))
        submitted_price = Decimal(str(parse_float(line.get("price"))))
        is_free = bool(line.get("is_free"))
        is_offer = bool(line.get("is_offer"))

        if is_free:
            if submitted_price != Decimal('0'):
                problems.append(f"Item {item_id} marked free but priced at {submitted_price}")
            continue  # free items don't contribute to the total

        if is_offer:
            recomputed_total += submitted_price * qty
            continue

        if not item_id:
            continue

        menu_doc = db.collection("menu_items").document(str(item_id)).get()
        if not menu_doc.exists:
            problems.append(f"Item {item_id} no longer exists in the menu")
            continue

        menu_item = menu_doc.to_dict() or {}
        if not menu_item.get("available", True):
            problems.append(f"Item {item_id} ({menu_item.get('name')}) is not currently available")

        real_price = effective_price(menu_item)
        if abs(real_price - submitted_price) > Decimal('0.01'):
            problems.append(
                f"Item {item_id} ({menu_item.get('name')}) priced at {submitted_price}, "
                f"but current price is {real_price}"
            )

        recomputed_total += real_price * qty

    return recomputed_total.quantize(Decimal('0.01')), problems


def validate_coupon(db: FirestoreClient, coupon_code: str | None, item_total: float | Decimal) -> tuple[Decimal, str | None]:
    """
    Re-checks a coupon the same way the Flutter admin/customer apps do
    (lib/screens/coupons_screen.dart, lib/providers/cart_provider.dart):
    must exist, be active, not expired, and item_total must meet its
    minimum order value.

    Returns (discount_amount, error_message). error_message is None if the
    coupon is valid (or none was applied); discount_amount is 0 if invalid.
    """
    if not coupon_code:
        return Decimal('0'), None

    matches = db.collection("coupons").where("code", "==", coupon_code).limit(1).get()
    if not matches:
        return Decimal('0'), f"Coupon '{coupon_code}' does not exist"

    coupon = matches[0].to_dict() or {}

    if not coupon.get("is_active", True):
        return Decimal('0'), f"Coupon '{coupon_code}' is not active"

    expiry = coupon.get("expiry_date")
    if expiry is not None:
        if isinstance(expiry, str):
            try:
                expiry = datetime.fromisoformat(expiry)
            except ValueError:
                expiry = None
        if isinstance(expiry, datetime):
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc):
                return Decimal('0'), f"Coupon '{coupon_code}' has expired"

    min_order = Decimal(str(coupon.get("min_order_value") or 0))
    item_total_decimal = Decimal(str(item_total))
    if item_total_decimal < min_order:
        return Decimal('0'), f"Coupon '{coupon_code}' requires a minimum order of {min_order}, cart is {item_total_decimal}"

    return Decimal(str(coupon.get("amount") or 0)), None
