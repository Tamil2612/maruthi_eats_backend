import inspect
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import main
from firebase_functions import https_fn
from order_validation import validate_coupon


class FakeDoc:
    def __init__(self, id, data):
        self.id = id
        self._d = data
        self.exists = data is not None
        self.reference = self

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class FakeRef:
    def __init__(self, db, coll, id):
        self.db = db
        self.coll = coll
        self.id = id

    def get(self, transaction=None):
        return FakeDoc(self.id, self.db.data.get(self.coll, {}).get(self.id))

    def set(self, data):
        self.db.data.setdefault(self.coll, {})[self.id] = data

    def update(self, data):
        doc = self.db.data.setdefault(self.coll, {}).setdefault(self.id, {})
        for k, v in data.items():
            if hasattr(v, '__class__') and v.__class__.__name__ == 'NumericIncrement':
                doc[k] = doc.get(k, 0) + v.value
            else:
                doc[k] = v


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs

    def limit(self, n):
        return FakeQuery(self.docs[:n])

    def where(self, field, op, value):
        if op == "==":
            filtered = [d for d in self.docs if (d.to_dict() or {}).get(field) == value]
        elif op in ("in", "array-contains-any"):
            filtered = [d for d in self.docs if (d.to_dict() or {}).get(field) in value]
        else:
            filtered = self.docs
        return FakeQuery(filtered)

    def order_by(self, field, direction=None):
        return self

    def get(self):
        return self.docs


class FakeCol:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def document(self, id=None):
        if id is None:
            self.db.counter += 1
            id = f"auto{self.db.counter}"
        return FakeRef(self.db, self.name, id)

    def where(self, field, op, value):
        rows = self.db.data.get(self.name, {})
        return FakeQuery([FakeDoc(i, d) for i, d in rows.items() if (d or {}).get(field) == value])


class FakeTransaction:
    def get(self, ref):
        return ref.get()

    def update(self, ref, data):
        ref.update(data)

    def set(self, ref, data):
        ref.set(data)


class FakeDB:
    def __init__(self):
        self.counter = 0
        future = datetime.now(timezone.utc) + timedelta(days=30)
        past = datetime.now(timezone.utc) - timedelta(days=1)

        self.data = {
            "menu_items": {
                "A": {"name": "Biryani", "price": 100, "available": True},
            },
            "coupons": {
                "c_first": {
                    "code": "FIRST50",
                    "amount": 50,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "is_first_order_only": True,
                },
                "c_once": {
                    "code": "ONCE30",
                    "amount": 30,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_uses_per_user": 1,
                },
                "c_three": {
                    "code": "TRIPLE10",
                    "amount": 10,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_uses_per_user": 3,
                },
                "c_global": {
                    "code": "GLOBAL20",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_global_uses": 2,
                    "current_global_uses": 0,
                },
                "c_legacy": {
                    "code": "LEGACY15",
                    "amount": 15,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                },
                "c_inactive": {
                    "code": "INACTIVE",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": False,
                    "expiry_date": future,
                },
                "c_expired": {
                    "code": "EXPIRED",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": past,
                },
            },
            "orders": {},
            "coupon_redemptions": {},
            "settings": {
                "restaurant": {
                    "is_open": True,
                    "timezone": "Asia/Kolkata",
                    "minimum_order_value": 100.0,
                    "delivery": {
                        "enabled": True,
                        "restaurant_latitude": 13.0827,
                        "restaurant_longitude": 80.2707,
                        "max_delivery_distance_km": 10.0,
                    }
                }
            },
        }

    def collection(self, name):
        return FakeCol(self, name)

    def transaction(self):
        return FakeTransaction()


def line(item_id="A", qty=2, price=100):
    return {"item_id": item_id, "name": "Biryani", "price": price, "qty": qty}


class CouponLimitsTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        main.firestore.client = lambda: self.db

    def place(self, items, uid="u_new", coupon_code=None, payment_mode="cod"):
        data = {
            "items": items,
            "payment_mode": payment_mode,
            "delivery_address": "123 Main St",
            "latitude": 13.0827,
            "longitude": 80.2707,
            "coupon_code": coupon_code,
        }
        req = SimpleNamespace(auth=SimpleNamespace(uid=uid), data=data)
        out = inspect.unwrap(main.place_order)(req)
        return out, self.db.data["orders"][out["order_id"]]

    # 1. First-time account successfully uses a first-order coupon
    def test_first_time_account_uses_first_order_coupon(self):
        out, order = self.place([line()], uid="u_new_1", coupon_code="FIRST50")
        self.assertEqual(order["coupon_discount"], 50.0)

    # 2. The same account is rejected when attempting to reuse that coupon or after placing an order
    def test_same_account_rejected_on_first_order_coupon_reuse(self):
        self.place([line()], uid="u_new_2", coupon_code="FIRST50")
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([line()], uid="u_new_2", coupon_code="FIRST50")
        self.assertIn("first order only", str(cm.exception.message))

    # 3. An existing customer with a completed qualifying order is rejected for a first-order-only coupon
    def test_existing_customer_rejected_for_first_order_coupon(self):
        # Create prior order for user
        self.db.data["orders"]["prior_1"] = {
            "customer_id": "u_existing",
            "order_status": "delivered",
            "payment_status": "paid",
        }
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([line()], uid="u_existing", coupon_code="FIRST50")
        self.assertIn("first order only", str(cm.exception.message))

    # 4. Two different eligible accounts can each use a once-per-account coupon
    def test_two_different_accounts_use_once_per_account_coupon(self):
        out1, ord1 = self.place([line()], uid="user_a", coupon_code="ONCE30")
        ord1["order_status"] = "delivered"
        out2, ord2 = self.place([line()], uid="user_b", coupon_code="ONCE30")
        ord2["order_status"] = "delivered"
        self.assertTrue(out1["order_id"])
        self.assertTrue(out2["order_id"])

        # Subsequent attempt by user_a is rejected
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([line()], uid="user_a", coupon_code="ONCE30")
        self.assertIn("already used", str(cm.exception.message))

    # 5. One account cannot exceed a configured three-use limit
    def test_three_use_limit_per_account(self):
        _, o1 = self.place([line()], uid="user_3", coupon_code="TRIPLE10")
        o1["order_status"] = "delivered"
        _, o2 = self.place([line()], uid="user_3", coupon_code="TRIPLE10")
        o2["order_status"] = "delivered"
        _, o3 = self.place([line()], uid="user_3", coupon_code="TRIPLE10")
        o3["order_status"] = "delivered"
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([line()], uid="user_3", coupon_code="TRIPLE10")
        self.assertIn("maximum usage limit", str(cm.exception.message))

    # 6. Legacy coupons without new fields retain their original behavior
    def test_legacy_coupons_retain_original_behavior(self):
        out1, o1 = self.place([line()], uid="u_leg", coupon_code="LEGACY15")
        o1["order_status"] = "delivered"
        out2, _ = self.place([line()], uid="u_leg", coupon_code="LEGACY15")
        self.assertEqual(out1["total"], 215.0)  # 200 - 15 + 30
        self.assertEqual(out2["total"], 215.0)

    # 7. Inactive and expired coupons are rejected
    def test_inactive_and_expired_coupons_rejected(self):
        with self.assertRaises(https_fn.HttpsError):
            self.place([line()], uid="u1", coupon_code="INACTIVE")
        with self.assertRaises(https_fn.HttpsError):
            self.place([line()], uid="u1", coupon_code="EXPIRED")

    # 8. Minimum cart value validation
    def test_minimum_cart_value_enforced(self):
        amt, err = validate_coupon(self.db, "FIRST50", 50, user_id="u1")
        self.assertIsNotNone(err)
        self.assertIn("minimum order", err)

    # 9. Cancelled or expired payments restore usage
    def test_cancelled_order_releases_coupon_reservation(self):
        out, order = self.place([line()], uid="u_cancel", coupon_code="ONCE30", payment_mode="upi")
        order_id = out["order_id"]

        # Cancel the order
        order_ref = self.db.collection("orders").document(order_id)
        order_dict = self.db.data["orders"][order_id]
        main._release_coupon_redemption_txn(self.db, self.db.transaction(), order_ref, order_dict)

        # Now user can reuse the coupon!
        out2, _ = self.place([line()], uid="u_cancel", coupon_code="ONCE30")
        self.assertTrue(out2["order_id"])

    # 10. Duplicate payment callbacks do not count redemption twice
    def test_duplicate_payment_callback_idempotency(self):
        out, order = self.place([line()], uid="u_dup", coupon_code="ONCE30", payment_mode="upi")
        order_id = out["order_id"]
        order_ref = self.db.collection("orders").document(order_id)

        # Mark paid first time
        main._mark_paid_txn(self.db.transaction(), order_ref, "pay_1")
        # Mark paid second time (duplicate callback)
        res2 = main._mark_paid_txn(self.db.transaction(), order_ref, "pay_1")
        self.assertEqual(res2, "already_paid")


if __name__ == "__main__":
    unittest.main()
