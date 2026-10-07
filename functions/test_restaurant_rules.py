"""
Offline unit & integration tests for restaurant business rules & settings:
  python -m unittest test_restaurant_rules -v
"""

import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main
from firebase_functions import https_fn
from restaurant_settings import (
    DEFAULT_RESTAURANT_SETTINGS,
    calculate_distance_km,
    calculate_delivery_fee,
    check_restaurant_availability,
    get_restaurant_local_time,
    is_restaurant_open_hours,
    is_special_closure,
    is_temporarily_paused,
    validate_delivery_distance,
    validate_minimum_order,
)


class FakeDoc:
    def __init__(self, id, data):
        self.id, self._d, self.exists = id, data, data is not None
        self.reference = self

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class FakeRef:
    def __init__(self, db, coll, id):
        self.db, self.coll, self.id = db, coll, id

    def get(self):
        return FakeDoc(self.id, self.db.data.get(self.coll, {}).get(self.id))

    def set(self, data):
        self.db.data.setdefault(self.coll, {})[self.id] = data

    def update(self, changes):
        doc = self.db.data.setdefault(self.coll, {}).get(self.id, {})
        doc.update(changes)
        self.db.data[self.coll][self.id] = doc


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
        self.db, self.name = db, name

    def document(self, id=None):
        if id is None:
            self.db.counter += 1
            id = f"auto{self.db.counter}"
        return FakeRef(self.db, self.name, id)

    def where(self, field, op, value):
        rows = self.db.data.get(self.name, {})
        return FakeQuery([FakeDoc(i, d) for i, d in rows.items() if d.get(field) == value])


class FakeDB:
    def __init__(self):
        self.counter = 0
        self.data = {
            "menu_items": {
                "A": {"name": "Biryani", "price": 100, "available": True},
                "B": {"name": "Naan", "price": 50, "available": True},
            },
            "offers": {},
            "coupons": {},
            "orders": {},
            "settings": {
                "restaurant": {
                    "is_open": True,
                    "timezone": "Asia/Kolkata",
                    "minimum_order_value": 150.0,
                    "pause": {"is_paused": False, "paused_until": None, "reason": ""},
                    "opening_hours": {
                        "monday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "tuesday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "wednesday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "thursday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "friday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "saturday": {"enabled": True, "open": "00:00", "close": "23:59"},
                        "sunday": {"enabled": True, "open": "00:00", "close": "23:59"},
                    },
                    "special_closures": [],
                    "delivery": {
                        "enabled": True,
                        "restaurant_latitude": 13.0827,
                        "restaurant_longitude": 80.2707,
                        "max_delivery_distance_km": 8.0,
                        "pricing": {
                            "type": "distance",
                            "slabs": [
                                {"up_to_km": 3.0, "fee": 30.0},
                                {"up_to_km": 5.0, "fee": 40.0},
                                {"up_to_km": 8.0, "fee": 60.0},
                            ],
                        },
                    },
                }
            },
        }

    def collection(self, name):
        return FakeCol(self, name)


class RestaurantBusinessRulesTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        main.firestore.client = lambda: self.db

    def place(self, items, lat=13.0827, lng=80.2707, **extra):
        data = {
            "items": items,
            "payment_mode": "cod",
            "delivery_address": "123 Main St",
            "latitude": lat,
            "longitude": lng,
            **extra,
        }
        req = SimpleNamespace(auth=SimpleNamespace(uid="u1"), data=data)
        out = inspect.unwrap(main.place_order)(req)
        return out, self.db.data["orders"][out["order_id"]]

    def rejects(self, items, lat=13.0827, lng=80.2707, **extra):
        with self.assertRaises(https_fn.HttpsError):
            self.place(items, lat=lat, lng=lng, **extra)

    # 1. Open restaurant accepts order
    def test_open_restaurant_accepts_order(self):
        out, order = self.place([{"item_id": "A", "qty": 2}])  # 2 * 100 = 200 >= 150
        self.assertEqual(order["order_status"], "placed")
        self.assertIn("delivery_fee", order)

    # 2. Closed restaurant rejects order
    def test_closed_restaurant_rejects_order(self):
        self.db.data["settings"]["restaurant"]["is_open"] = False
        self.rejects([{"item_id": "A", "qty": 2}])

    # 3. Outside opening hours rejects order
    def test_outside_opening_hours_rejects_order(self):
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            self.db.data["settings"]["restaurant"]["opening_hours"][day] = {"enabled": False}
        self.rejects([{"item_id": "A", "qty": 2}])

    # 4. Manual pause rejects order
    def test_manual_pause_rejects_order(self):
        self.db.data["settings"]["restaurant"]["pause"] = {
            "is_paused": True,
            "paused_until": None,
            "reason": "Kitchen Overloaded",
        }
        self.rejects([{"item_id": "A", "qty": 2}])

    # 5. Expired pause allows order
    def test_expired_pause_allows_order(self):
        past_utc = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.db.data["settings"]["restaurant"]["pause"] = {
            "is_paused": True,
            "paused_until": past_utc,
            "reason": "Temporary hold",
        }
        out, order = self.place([{"item_id": "A", "qty": 2}])
        self.assertEqual(order["order_status"], "placed")

    # 6. Special closure rejects order
    def test_special_closure_rejects_order(self):
        tz_name = self.db.data["settings"]["restaurant"]["timezone"]
        local_today = get_restaurant_local_time(tz_name).strftime("%Y-%m-%d")
        self.db.data["settings"]["restaurant"]["special_closures"] = [
            {"date": local_today, "closed": True, "reason": "Festival Holiday"}
        ]
        self.rejects([{"item_id": "A", "qty": 2}])

    # 7. Minimum order rejects low subtotal
    def test_minimum_order_rejects_low_subtotal(self):
        self.rejects([{"item_id": "A", "qty": 1}])  # 100 < 150

    # 8. Minimum order accepts valid subtotal
    def test_minimum_order_accepts_valid_subtotal(self):
        out, order = self.place([{"item_id": "A", "qty": 2}])  # 200 >= 150
        self.assertEqual(order["item_total"], 200.0)

    # 9. Delivery distance inside radius accepted
    def test_delivery_distance_inside_radius_accepted(self):
        # Coordinates ~2 km from 13.0827, 80.2707
        out, order = self.place([{"item_id": "A", "qty": 2}], lat=13.0950, lng=80.2707)
        self.assertLessEqual(order["delivery_distance_km"], 8.0)

    # 10. Delivery distance outside radius rejected
    def test_delivery_distance_outside_radius_rejected(self):
        # Coordinates ~100 km away
        self.rejects([{"item_id": "A", "qty": 2}], lat=12.0000, lng=79.0000)

    # 11. Invalid latitude rejected
    def test_invalid_latitude_rejected(self):
        self.rejects([{"item_id": "A", "qty": 2}], lat=100.0, lng=80.2707)

    # 12. Invalid longitude rejected
    def test_invalid_longitude_rejected(self):
        self.rejects([{"item_id": "A", "qty": 2}], lat=13.0827, lng=200.0)

    # 13. Delivery fee 0-3 km = 30
    def test_delivery_fee_0_to_3_km(self):
        # Same spot = 0 km
        out, order = self.place([{"item_id": "A", "qty": 2}], lat=13.0827, lng=80.2707)
        self.assertEqual(order["delivery_fee"], 30.0)

    # 14. Delivery fee >3-5 km = 40
    def test_delivery_fee_3_to_5_km(self):
        # ~4 km away
        dist = calculate_distance_km(13.0827, 80.2707, 13.1180, 80.2707)
        self.assertTrue(3.0 < dist <= 5.0)
        fee = calculate_delivery_fee(self.db.data["settings"]["restaurant"]["delivery"], dist)
        self.assertEqual(fee, 40.0)

    # 15. Delivery fee >5-8 km = 60
    def test_delivery_fee_5_to_8_km(self):
        # ~6.5 km away
        dist = calculate_distance_km(13.0827, 80.2707, 13.1410, 80.2707)
        self.assertTrue(5.0 < dist <= 8.0)
        fee = calculate_delivery_fee(self.db.data["settings"]["restaurant"]["delivery"], dist)
        self.assertEqual(fee, 60.0)

    # 16. >8 km rejected
    def test_greater_than_8km_rejected(self):
        dist = calculate_distance_km(13.0827, 80.2707, 13.2000, 80.2707)
        self.assertGreaterThan(dist, 8.0)
        valid, msg = validate_delivery_distance(self.db.data["settings"]["restaurant"]["delivery"], dist)
        self.assertFalse(valid)

    # 17. Client-provided delivery fee is ignored
    def test_client_provided_delivery_fee_ignored(self):
        out, order = self.place([{"item_id": "A", "qty": 2}], delivery_fee=0.0)
        self.assertEqual(order["delivery_fee"], 30.0)  # Server calculated

    # 18. Client-provided distance is ignored
    def test_client_provided_distance_ignored(self):
        out, order = self.place([{"item_id": "A", "qty": 2}], distance_km=0.1)
        self.assertIn("delivery_distance_km", order)

    # 19. Historical order retains original delivery fee
    def test_historical_order_retains_original_delivery_fee(self):
        out, order = self.place([{"item_id": "A", "qty": 2}])
        orig_fee = order["delivery_fee"]

        # Admin changes slabs in settings
        self.db.data["settings"]["restaurant"]["delivery"]["pricing"]["slabs"] = [
            {"up_to_km": 8.0, "fee": 100.0}
        ]

        # Historical order document in DB retains orig_fee
        stored_order = self.db.data["orders"][out["order_id"]]
        self.assertEqual(stored_order["delivery_fee"], orig_fee)

    # 20. Audit does not flag legitimate orders placed at various distance slabs (1km, 4km, 6.5km)
    def test_audit_does_not_flag_legitimate_slab_orders(self):
        # 1 km (~30 fee)
        out1, order1 = self.place([{"item_id": "A", "qty": 2}], lat=13.0827, lng=80.2707)
        req_event1 = SimpleNamespace(data=SimpleNamespace(to_dict=lambda: order1), params={"order_id": out1["order_id"]})
        inspect.unwrap(main.on_order_created)(req_event1)
        self.assertEqual(self.db.data["orders"][out1["order_id"]]["validation_status"], "ok")

        # 4 km (~40 fee)
        out2, order2 = self.place([{"item_id": "A", "qty": 2}], lat=13.1180, lng=80.2707)
        req_event2 = SimpleNamespace(data=SimpleNamespace(to_dict=lambda: order2), params={"order_id": out2["order_id"]})
        inspect.unwrap(main.on_order_created)(req_event2)
        self.assertEqual(self.db.data["orders"][out2["order_id"]]["validation_status"], "ok")

        # 6.5 km (~60 fee)
        out3, order3 = self.place([{"item_id": "A", "qty": 2}], lat=13.1410, lng=80.2707)
        req_event3 = SimpleNamespace(data=SimpleNamespace(to_dict=lambda: order3), params={"order_id": out3["order_id"]})
        inspect.unwrap(main.on_order_created)(req_event3)
        self.assertEqual(self.db.data["orders"][out3["order_id"]]["validation_status"], "ok")

    # 21. Abandon UPI, then COD immediately works
    def test_abandon_upi_then_cod_immediately_works(self):
        out1, order1 = self.place([{"item_id": "A", "qty": 2}], payment_mode="upi")
        self.assertEqual(order1["order_status"], "pending_payment")

        out2, order2 = self.place([{"item_id": "A", "qty": 2}], payment_mode="cod")
        self.assertEqual(order2["order_status"], "placed")

    # 22. Many unpaid attempts are limited
    def test_many_unpaid_attempts_limited(self):
        now = datetime.now(timezone.utc)
        for i in range(5):
            self.db.data["orders"][f"p{i}"] = {
                "customer_id": "u1",
                "order_status": "pending_payment",
                "created_at": now,
            }

        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([{"item_id": "A", "qty": 2}], payment_mode="upi")
        self.assertIn("Too many unpaid payment attempts", str(cm.exception.message))

    def assertGreaterThan(self, a, b):
        self.assertTrue(a > b)


if __name__ == "__main__":
    unittest.main()
