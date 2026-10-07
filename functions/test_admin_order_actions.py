"""
Offline unit & integration tests for Admin Callable Cloud Functions:
  - update_order_status
  - mark_cod_collected
  - cancel_order
  - retry_refund
  - submit_order_rating
"""

import inspect
import unittest
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

    def update(self, changes):
        doc = self.db.data.setdefault(self.coll, {}).get(self.id, {})
        doc.update(changes)
        self.db.data[self.coll][self.id] = doc

    def collection(self, name):
        return FakeCol(self.db, f"{self.coll}/{self.id}/{name}")


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs

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

    def add(self, data):
        self.db.counter += 1
        id = f"auto{self.db.counter}"
        self.db.data.setdefault(self.name, {})[id] = data
        return FakeRef(self.db, self.name, id)

    def get(self):
        rows = self.db.data.get(self.name, {})
        return [FakeDoc(i, d) for i, d in rows.items()]


class FakeDB:
    def __init__(self):
        self.counter = 0
        self.data = {
            "staff": {
                "staff1": {"role": "manager", "name": "Admin Staff"}
            },
            "orders": {
                "o1": {
                    "customer_id": "cust1",
                    "order_status": "placed",
                    "payment_mode": "cod",
                    "payment_status": "cod_pending",
                    "refund_status": "failed",
                    "total": 200.0,
                },
                "o2": {
                    "customer_id": "cust1",
                    "order_status": "delivered",
                    "payment_mode": "upi",
                    "payment_status": "paid",
                    "total": 150.0,
                }
            }
        }

    def collection(self, name):
        return FakeCol(self, name)


class AdminOrderActionsTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        main.firestore.client = lambda: self.db

    def call_func(self, func, uid="staff1", data=None):
        req = SimpleNamespace(auth=SimpleNamespace(uid=uid), data=data or {})
        return inspect.unwrap(func)(req)

    # 1. Non-staff requests rejected
    def test_non_staff_rejected_for_admin_actions(self):
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.call_func(main.update_order_status, uid="cust1", data={"order_id": "o1", "order_status": "preparing"})
        self.assertIn("Access denied", cm.exception.message)

    # 2. Staff update order status
    def test_staff_update_order_status_success(self):
        res = self.call_func(main.update_order_status, uid="staff1", data={"order_id": "o1", "order_status": "preparing"})
        self.assertEqual(res["order_status"], "preparing")
        self.assertEqual(self.db.data["orders"]["o1"]["order_status"], "preparing")
        self.assertEqual(self.db.data["orders"]["o1"]["updated_by"], "staff1")

    # 3. Mark COD collected
    def test_mark_cod_collected_success(self):
        res = self.call_func(main.mark_cod_collected, uid="staff1", data={"order_id": "o1"})
        self.assertEqual(res["payment_status"], "paid")
        self.assertEqual(self.db.data["orders"]["o1"]["payment_status"], "paid")
        self.assertEqual(self.db.data["orders"]["o1"]["updated_by"], "staff1")

    # 4. Cancel order
    def test_cancel_order_success(self):
        res = self.call_func(main.cancel_order, uid="staff1", data={"order_id": "o1", "reason": "Out of stock"})
        self.assertEqual(res["order_status"], "cancelled")
        self.assertEqual(self.db.data["orders"]["o1"]["order_status"], "cancelled")
        self.assertEqual(self.db.data["orders"]["o1"]["cancel_reason"], "Out of stock")

    # 5. Retry refund
    def test_retry_refund_success(self):
        res = self.call_func(main.retry_refund, uid="staff1", data={"order_id": "o1"})
        self.assertEqual(res["refund_status"], "retry_requested")
        self.assertEqual(self.db.data["orders"]["o1"]["refund_status"], "retry_requested")

    # 6. Submit rating for delivered order
    def test_submit_order_rating_success(self):
        res = self.call_func(main.submit_order_rating, uid="cust1", data={"order_id": "o2", "rating": 5})
        self.assertEqual(res["rating"], 5)
        self.assertEqual(self.db.data["orders"]["o2"]["rating"], 5)


if __name__ == "__main__":
    unittest.main()
