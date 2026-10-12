"""
Offline tests for the order-action callables:
  update_order_status / mark_cod_collected / cancel_order / retry_refund / submit_order_rating

Run from functions/:  python -m unittest test_admin_order_actions -v
"""

import inspect
import unittest
from types import SimpleNamespace

# The status callables use Firestore transactions. In these offline tests the
# decorator is replaced by a pass-through and FakeTxn applies the writes directly.
import firebase_admin.firestore as _fs
import google.cloud.firestore_v1.transaction as _txn

_fs.transactional = lambda fn: fn
_txn.transactional = lambda fn: fn

import main  # noqa: E402
from firebase_functions import https_fn  # noqa: E402


class FakeDoc:
    def __init__(self, id, data):
        self.id, self._d, self.exists = id, data, data is not None
        self.reference = self

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class FakeRef:
    def __init__(self, db, coll, id):
        self.db, self.coll, self.id = db, coll, id

    def get(self, transaction=None):
        return FakeDoc(self.id, self.db.data.get(self.coll, {}).get(self.id))

    def set(self, data):
        self.db.data.setdefault(self.coll, {})[self.id] = data

    def update(self, changes):
        doc = self.db.data.setdefault(self.coll, {}).get(self.id, {})
        doc.update(changes)
        self.db.data[self.coll][self.id] = doc

    def collection(self, name):
        return FakeCol(self.db, f"{self.coll}/{self.id}/{name}")


class FakeTxn:
    def update(self, ref, changes):
        ref.update(changes)

    def set(self, ref, data):
        ref.set(data)


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
            "staff": {"staff1": {"role": "manager", "name": "Admin Staff"}},
            "orders": {
                # cash order that is still waiting to be accepted
                "o1": {"customer_id": "cust1", "order_status": "placed", "payment_mode": "cod",
                       "payment_status": "cod_pending", "refund_status": "failed", "total": 200.0},
                # paid UPI order that is already delivered
                "o2": {"customer_id": "cust1", "order_status": "delivered", "payment_mode": "upi",
                       "payment_status": "paid", "total": 150.0},
                # UPI order whose payment has not been confirmed
                "o3": {"customer_id": "cust1", "order_status": "pending_payment", "payment_mode": "upi",
                       "payment_status": "pending", "total": 120.0},
                # paid UPI order, restaurant is cooking it
                "o4": {"customer_id": "cust1", "order_status": "preparing", "payment_mode": "upi",
                       "payment_status": "paid", "total": 99.0},
            },
        }

    def collection(self, name):
        return FakeCol(self, name)

    def transaction(self):
        return FakeTxn()


class OrderActionTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        main.firestore.client = lambda: self.db

    def call_func(self, func, uid="staff1", data=None):
        req = SimpleNamespace(auth=SimpleNamespace(uid=uid), data=data or {})
        return inspect.unwrap(func)(req)

    def order(self, oid="o1"):
        return self.db.data["orders"][oid]

    def rejects(self, func, uid="staff1", data=None, contains=None):
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.call_func(func, uid=uid, data=data)
        if contains:
            self.assertIn(contains, cm.exception.message)

    # ---- who may do what -------------------------------------------------------
    def test_non_staff_rejected_for_admin_actions(self):
        self.rejects(main.update_order_status, uid="cust1",
                     data={"order_id": "o1", "order_status": "preparing"}, contains="Access denied")

    # ---- status flow -------------------------------------------------------------
    def test_staff_moves_order_forward_one_step_at_a_time(self):
        res = self.call_func(main.update_order_status, data={"order_id": "o1", "order_status": "preparing"})
        self.assertEqual(res["order_status"], "preparing")
        self.assertEqual(self.order()["order_status"], "preparing")
        self.assertEqual(self.order()["updated_by"], "staff1")
        self.call_func(main.update_order_status, data={"order_id": "o1", "order_status": "out_for_delivery"})
        self.call_func(main.update_order_status, data={"order_id": "o1", "order_status": "delivered"})
        self.assertEqual(self.order()["order_status"], "delivered")

    def test_every_change_is_logged(self):
        self.call_func(main.update_order_status, data={"order_id": "o1", "order_status": "preparing"})
        logs = list(self.db.data["orders/o1/status_log"].values())
        self.assertEqual([l["status"] for l in logs], ["preparing"])
        self.assertEqual(logs[0]["changed_by"], "staff1")

    def test_steps_cannot_be_skipped_or_reversed(self):
        self.rejects(main.update_order_status, data={"order_id": "o1", "order_status": "delivered"})
        self.rejects(main.update_order_status, data={"order_id": "o2", "order_status": "preparing"})
        self.rejects(main.update_order_status, data={"order_id": "o1", "order_status": "placed"})
        self.assertEqual(self.order("o1")["order_status"], "placed")
        self.assertEqual(self.order("o2")["order_status"], "delivered")

    def test_unpaid_upi_order_cannot_be_accepted(self):
        # still waiting for the payment: not a step the flow allows at all
        self.rejects(main.update_order_status, data={"order_id": "o3", "order_status": "preparing"})
        self.assertEqual(self.order("o3")["order_status"], "pending_payment")
        # second line of defence: even an order that somehow reached "placed" unpaid is refused
        self.order("o3")["order_status"] = "placed"
        self.rejects(main.update_order_status, data={"order_id": "o3", "order_status": "preparing"},
                     contains="not been paid")
        self.assertEqual(self.order("o3")["order_status"], "placed")

    # ---- cash on delivery ----------------------------------------------------------
    def test_cod_collected_only_once_out_for_delivery(self):
        self.rejects(main.mark_cod_collected, data={"order_id": "o1"}, contains="out for delivery")
        self.order("o1")["order_status"] = "out_for_delivery"
        res = self.call_func(main.mark_cod_collected, data={"order_id": "o1"})
        self.assertEqual(res["payment_status"], "cod_collected")     # the value both apps understand
        self.assertEqual(self.order("o1")["payment_status"], "cod_collected")
        again = self.call_func(main.mark_cod_collected, data={"order_id": "o1"})   # harmless repeat
        self.assertEqual(again["payment_status"], "cod_collected")

    def test_cod_collected_rejected_for_upi_orders(self):
        self.rejects(main.mark_cod_collected, data={"order_id": "o2"}, contains="Cash on Delivery")

    # ---- cancelling ------------------------------------------------------------------
    def test_staff_cancel_with_reason(self):
        res = self.call_func(main.cancel_order, data={"order_id": "o1", "reason": "Out of stock"})
        self.assertEqual(res["order_status"], "cancelled")
        self.assertEqual(self.order()["cancel_reason"], "Out of stock")

    def test_staff_can_cancel_an_order_in_progress_but_not_a_delivered_one(self):
        self.call_func(main.cancel_order, data={"order_id": "o4"})
        self.assertEqual(self.order("o4")["order_status"], "cancelled")
        self.rejects(main.cancel_order, data={"order_id": "o2"}, contains="cannot be cancelled")
        self.assertEqual(self.order("o2")["order_status"], "delivered")   # and so no refund is triggered

    def test_customer_cancels_only_their_own_placed_order(self):
        self.call_func(main.cancel_order, uid="cust1", data={"order_id": "o1"})
        self.assertEqual(self.order("o1")["order_status"], "cancelled")
        self.rejects(main.cancel_order, uid="cust1", data={"order_id": "o4"}, contains="in progress")
        self.rejects(main.cancel_order, uid="stranger", data={"order_id": "o1"}, contains="Permission denied")

    # ---- refunds and ratings -----------------------------------------------------------
    def test_retry_refund_success(self):
        res = self.call_func(main.retry_refund, data={"order_id": "o1"})
        self.assertEqual(res["refund_status"], "retry_requested")
        self.assertEqual(self.order("o1")["refund_status"], "retry_requested")

    def test_submit_order_rating_success(self):
        res = self.call_func(main.submit_order_rating, uid="cust1", data={"order_id": "o2", "rating": 5})
        self.assertEqual(res["rating"], 5)
        self.assertEqual(self.order("o2")["rating"], 5)


if __name__ == "__main__":
    unittest.main()