"""Offline tests for place_order (no Firebase needed): python -m unittest test_place_order -v"""
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main
from firebase_functions import https_fn


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
        self.db, self.name, self.n = db, name, 0

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
        future = datetime.now(timezone.utc) + timedelta(days=30)
        self.data = {
            "menu_items": {
                "A": {"name": "Biryani", "price": 100, "available": True},
                "B": {"name": "Naan", "price": 50, "has_discount": True, "discount_price": 40, "available": True},
                "C": {"name": "Sold out", "price": 10, "available": False},
                "X": {"name": "Lobster", "price": 900, "available": True},
            },
            "offers": {
                "combo1": {"type": "combo", "title": "Family Combo", "combo_price": 199, "is_active": True,
                           "bundle_items": [{"item_id": "A", "item_name": "Biryani", "qty": 1}]},
                "bogo1": {"type": "bogo", "title": "Buy Biryani Get Naan", "is_active": True,
                          "buy_item_id": "A", "buy_item_name": "Biryani", "buy_qty": 1,
                          "get_item_id": "B", "get_item_name": "Naan", "get_qty": 1},
                "dead": {"type": "combo", "title": "Old", "combo_price": 1, "is_active": False},
            },
            "coupons": {"c1": {"code": "SAVE20", "amount": 20, "min_order_value": 100, "is_active": True,
                               "expiry_date": future}},
            "orders": {},
            "settings": {
                "restaurant": {
                    "is_open": True,
                    "timezone": "Asia/Kolkata",
                    "minimum_order_value": 150.0,
                    "delivery": {
                        "enabled": True,
                        "restaurant_latitude": 13.0827,
                        "restaurant_longitude": 80.2707,
                        "max_delivery_distance_km": 8.0,
                    }
                }
            },
        }

    def collection(self, name):
        return FakeCol(self, name)


def line(item_id, qty=1, price=1, **kw):
    return {"item_id": item_id, "name": "client name", "price": price, "qty": qty,
            "is_offer": False, "is_free": False, **kw}


class PlaceOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        main.firestore.client = lambda: self.db

    def place(self, items, **extra):
        data = {"items": items, "payment_mode": "cod", "delivery_address": "12 Test Street", "latitude": 13.0827, "longitude": 80.2707, **extra}
        req = SimpleNamespace(auth=SimpleNamespace(uid="u1"), data=data)
        out = inspect.unwrap(main.place_order)(req)
        return out, self.db.data["orders"][out["order_id"]]

    def rejects(self, items, **extra):
        with self.assertRaises(https_fn.HttpsError):
            self.place(items, **extra)

    # ---- pricing from the menu, never from the app --------------------------
    def test_regular_items_use_menu_prices(self):
        out, order = self.place([line("A", 2, price=1), line("B", 1, price=1)])
        self.assertEqual(order["item_total"], 240.0)       # 2*100 + 1*40 (discount)
        self.assertEqual(out["total"], 270.0)              # + 30 delivery
        self.assertEqual([i["price"] for i in order["items"]], [100.0, 40.0])

    def test_cheap_client_price_ignored(self):
        _, order = self.place([line("X", 1, price=0.01)])
        self.assertEqual(order["item_total"], 900.0)

    # ---- quantities -----------------------------------------------------------
    def test_bad_quantities_rejected(self):
        for q in (0, -5, 51, "abc"):
            self.rejects([line("A", q)])

    def test_too_many_lines_rejected(self):
        self.rejects([line("A") for _ in range(60)])

    def test_unavailable_or_unknown_item_rejected(self):
        self.rejects([line("C")])
        self.rejects([line("NOPE")])

    # ---- combos ---------------------------------------------------------------
    def test_combo_price_comes_from_offer(self):
        _, order = self.place([line("offer_combo1", 2, price=1, is_offer=True, offer_id="combo1")])
        self.assertEqual(order["item_total"], 398.0)
        self.assertTrue(order["items"][0].get("is_combo"))
        self.assertEqual(order["items"][0]["bundle_items"][0]["item_name"], "Biryani")

    def test_inactive_or_unknown_offer_rejected(self):
        self.rejects([line("o", 1, is_offer=True, offer_id="dead")])
        self.rejects([line("o", 1, is_offer=True, offer_id="ghost")])

    # ---- BOGO -----------------------------------------------------------------
    def bogo(self, paid, free, free_item="B"):
        return [
            line("bogo_bogo1_buy", paid, is_offer=True, offer_id="bogo1", parent_offer_id="bogo_bogo1"),
            line(free_item if False else "bogo_bogo1_get", free, price=0, is_offer=True, is_free=True,
                 offer_id="bogo1", parent_offer_id="bogo_bogo1", name="Lobster"),
        ]

    def test_valid_bogo(self):
        _, order = self.place(self.bogo(2, 2))
        self.assertEqual(order["item_total"], 200.0)       # only the paid Biryani counts
        free = [i for i in order["items"] if i["is_free"]][0]
        self.assertEqual(free["price"], 0.0)
        self.assertEqual(free["name"], "Naan")             # name from the offer, not the app

    def test_free_quantity_cannot_exceed_paid(self):
        self.rejects(self.bogo(1, 50))
        self.rejects(self.bogo(2, 3))

    def test_free_item_without_paid_item_rejected(self):
        self.rejects([self.bogo(1, 1)[1]])

    def test_free_flag_on_non_bogo_offer_rejected(self):
        self.rejects([line("x", 1, price=0, is_offer=True, is_free=True, offer_id="combo1")])

    # ---- everything else -----------------------------------------------------
    def test_payment_mode_and_address(self):
        self.rejects([line("A")], payment_mode="cash")
        self.rejects([line("A")], delivery_address="   ")

    def test_coupon(self):
        out, order = self.place([line("A", 2)], coupon_code="SAVE20")
        self.assertEqual(order["coupon_discount"], 20.0)
        self.assertEqual(out["total"], 210.0)              # 200 - 20 + 30
        self.rejects([line("A", 2)], coupon_code="NOPE")
        self.rejects([line("A", 2)], coupon_code={"x": 1})

    def test_upi_order_is_pending_payment(self):
        _, order = self.place([line("A", qty=2)], payment_mode="upi")
        self.assertEqual((order["order_status"], order["payment_status"]), ("pending_payment", "pending"))

    def test_stored_order_has_only_known_fields(self):
        _, order = self.place([line("A", qty=2, junk="x", is_admin=True)])
        self.assertNotIn("junk", order["items"][0])


if __name__ == "__main__":
    unittest.main()
