"""
Offline unit & integration tests for delete_account Callable Cloud Function:
  python -m unittest test_delete_account -v
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

    def collection(self, name):
        return FakeCol(self.db, f"{self.coll}/{self.id}/{name}")

    def delete(self):
        if self.coll in self.db.data and self.id in self.db.data[self.coll]:
            del self.db.data[self.coll][self.id]


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

    def get(self):
        rows = self.db.data.get(self.name, {})
        return [FakeDoc(i, d) for i, d in rows.items()]


class FakeBatch:
    def __init__(self, db):
        self.db = db
        self.ops = []

    def delete(self, ref):
        self.ops.append(("delete", ref, None))

    def update(self, ref, updates):
        self.ops.append(("update", ref, updates))

    def commit(self):
        for op, ref, updates in self.ops:
            coll = getattr(ref, "coll", "orders")
            doc_id = getattr(ref, "id", None)
            if op == "delete":
                if coll in self.db.data and doc_id in self.db.data[coll]:
                    del self.db.data[coll][doc_id]
            elif op == "update":
                if coll in self.db.data and doc_id in self.db.data[coll]:
                    self.db.data[coll][doc_id].update(updates)
        self.ops.clear()


class FakeAuth:
    def __init__(self):
        self.deleted_uids = []

    def delete_user(self, uid):
        self.deleted_uids.append(uid)


class FakeDB:
    def __init__(self):
        self.counter = 0
        self.data = {
            "users": {
                "u1": {"name": "John Doe", "email": "john@example.com"}
            },
            "orders": {
                "o1": {
                    "customer_id": "u1",
                    "order_status": "delivered",
                    "refund_status": "none",
                    "delivery_address": "123 Secret St, Apt 4B",
                    "address_label": "Home",
                    "latitude": 13.0827,
                    "longitude": 80.2707,
                    "total": 250.0,
                }
            }
        }

    def collection(self, name):
        return FakeCol(self, name)

    def batch(self):
        return FakeBatch(self)


class DeleteAccountTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.auth = FakeAuth()
        main.firestore.client = lambda: self.db
        main.auth = self.auth

    def call_delete(self, uid="u1"):
        req = SimpleNamespace(auth=SimpleNamespace(uid=uid))
        return inspect.unwrap(main.delete_account)(req)

    # 1. Active order blocks deletion
    def test_active_order_blocks_account_deletion(self):
        self.db.data["orders"]["o1"]["order_status"] = "preparing"
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.call_delete("u1")
        self.assertIn("You have an order or refund in progress", cm.exception.message)
        self.assertEqual(len(self.auth.deleted_uids), 0)

    # 2. No personal data left on orders
    def test_no_personal_data_left_on_orders_after_deletion(self):
        res = self.call_delete("u1")
        self.assertEqual(res["status"], "success")

        order = self.db.data["orders"]["o1"]
        self.assertEqual(order["customer_id"], "anonymized_u1")
        self.assertEqual(order["delivery_address"], "Anonymized Address")
        self.assertEqual(order["address_label"], "Anonymized")
        self.assertIsNone(order["latitude"])
        self.assertIsNone(order["longitude"])
        self.assertEqual(order["total"], 250.0)  # Financial total preserved

    # 3. Auth deletion called once
    def test_auth_deletion_called_once(self):
        self.call_delete("u1")
        self.assertEqual(self.auth.deleted_uids, ["u1"])


if __name__ == "__main__":
    unittest.main()
