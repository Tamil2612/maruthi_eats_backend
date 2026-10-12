"""
Offline tests for payment settlement:
  - _settle_payment captures a payment Razorpay only *authorized*
  - reconcile_pending_payments places orders that were paid but never verified

Run from functions/:  python -m unittest test_payment_settlement -v
"""

import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import firebase_admin.firestore as _fs
import google.cloud.firestore_v1.transaction as _txn

_fs.transactional = lambda fn: fn
_txn.transactional = lambda fn: fn

import main  # noqa: E402

DELETE = _fs.DELETE_FIELD
SERVER_TS = _fs.SERVER_TIMESTAMP


class Snap:
    def __init__(self, id, data, ref=None):
        self.id, self._d, self.exists, self.reference = id, data, data is not None, ref

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class Ref:
    def __init__(self, db, coll, id):
        self.db, self.coll, self.id = db, coll, id

    def get(self, transaction=None):
        return Snap(self.id, self.db.data.get(self.coll, {}).get(self.id), self)

    def update(self, changes):
        doc = self.db.data[self.coll][self.id]
        for k, v in changes.items():
            if v is DELETE:
                doc.pop(k, None)
            elif v is SERVER_TS:
                doc[k] = "<ts>"
            else:
                doc[k] = v


class Txn:
    def update(self, ref, changes):
        ref.update(changes)


class Query:
    def __init__(self, db, coll, rows):
        self.db, self.coll, self.rows = db, coll, rows

    def get(self):
        return [Snap(i, self.db.data[self.coll][i], Ref(self.db, self.coll, i)) for i in self.rows]


class Col:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, id):
        return Ref(self.db, self.name, id)

    def where(self, field, op, value):
        rows = [i for i, d in self.db.data.get(self.name, {}).items() if d.get(field) == value]
        return Query(self.db, self.name, rows)


class DB:
    def __init__(self):
        self.data = {"orders": {}}

    def collection(self, name):
        return Col(self, name)

    def transaction(self):
        return Txn()


class FakeRazorpay:
    def __init__(self, payments=None, fail=False):
        self.created = []
        self.captures = []
        self.payments_by_order = payments or {}
        self.by_id = {p["id"]: p for items in self.payments_by_order.values() for p in items}
        self.fail = fail
        self.auto_capture_wins = False
        self.order = SimpleNamespace(create=self._create, payments=self._payments)
        self.payment = SimpleNamespace(fetch=self._fetch, capture=self._capture)

    def _fetch(self, payment_id):
        if self.fail:
            raise RuntimeError("razorpay down")
        return dict(self.by_id[payment_id])

    def _capture(self, payment_id, amount, data=None):
        if self.auto_capture_wins:
            raise RuntimeError("payment already captured")
        self.captures.append((payment_id, amount))
        self.by_id[payment_id]["status"] = "captured"
        return dict(self.by_id[payment_id])

    def _create(self, data):
        if self.fail:
            raise RuntimeError("razorpay down")
        self.created.append(data)
        return {"id": f"order_{len(self.created)}"}

    def _payments(self, order_id):
        if self.fail:
            raise RuntimeError("razorpay down")
        return {"items": self.payments_by_order.get(order_id, [])}


def upi_order(**kw):
    base = {"customer_id": "c1", "payment_mode": "upi", "payment_status": "pending",
            "order_status": "pending_payment", "razorpay_order_id": "order_1", "razorpay_amount": 27000,
            "total": 270.0, "created_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
    base.update(kw)
    return base



class SettleTests(unittest.TestCase):
    def setUp(self):
        main.time.sleep = lambda s: None          # no real waiting in tests

    def rz(self, status, **kw):
        pay = {"id": "pay_1", "status": status, "amount": 27000}
        pay.update(kw)
        return FakeRazorpay({"order_1": [pay]})

    def test_captured_payment_is_returned_as_is(self):
        rz = self.rz("captured")
        self.assertEqual(main._settle_payment(rz, "pay_1", 27000)["status"], "captured")
        self.assertEqual(rz.captures, [])

    def test_authorized_payment_is_captured(self):
        rz = self.rz("authorized")
        self.assertEqual(main._settle_payment(rz, "pay_1", 27000)["status"], "captured")
        self.assertEqual(rz.captures, [("pay_1", 27000)])

    def test_capture_race_with_auto_capture_is_not_an_error(self):
        rz = self.rz("authorized")
        rz.auto_capture_wins = True
        rz.by_id["pay_1"]["status"] = "authorized"
        # capture raises, but the re-fetch shows the real (still authorized) status:
        self.assertEqual(main._settle_payment(rz, "pay_1", 27000)["status"], "authorized")

    def test_failed_payment_is_reported_immediately(self):
        rz = self.rz("failed")
        self.assertEqual(main._settle_payment(rz, "pay_1", 27000)["status"], "failed")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        main.time.sleep = lambda s: None
        self.db = DB()
        main.firestore.client = lambda: self.db

    def use(self, status="captured", amount=27000):
        self.rzp = FakeRazorpay({"order_1": [{"id": "pay_1", "status": status, "amount": amount}]})
        main._razorpay_client = lambda: (self.rzp, "k")

    def add(self, oid="o1", **kw):
        self.db.data["orders"][oid] = upi_order(**kw)

    def doc(self, oid="o1"):
        return self.db.data["orders"][oid]

    def run_job(self):
        inspect.unwrap(main.reconcile_pending_payments)(None)

    def test_paid_but_unverified_order_is_placed(self):
        self.use("captured"); self.add()
        self.run_job()
        self.assertEqual((self.doc()["order_status"], self.doc()["payment_status"]), ("placed", "paid"))
        self.assertEqual(self.doc()["razorpay_payment_id"], "pay_1")

    def test_authorized_payment_is_captured_then_placed(self):
        self.use("authorized"); self.add()
        self.run_job()
        self.assertEqual(self.rzp.captures, [("pay_1", 27000)])
        self.assertEqual(self.doc()["order_status"], "placed")

    def test_unpaid_order_is_left_alone(self):
        self.use("captured"); self.add(razorpay_order_id="order_9")
        self.run_job()
        self.assertEqual(self.doc()["order_status"], "pending_payment")

    def test_wrong_amount_is_never_accepted(self):
        self.use("captured", amount=100); self.add()
        self.run_job()
        self.assertEqual(self.doc()["order_status"], "pending_payment")

    def test_very_new_attempts_are_skipped(self):
        self.use("captured")
        self.add(created_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        self.run_job()
        self.assertEqual(self.doc()["order_status"], "pending_payment")

    def test_latest_payment_attempt_time_is_used(self):
        self.use("captured")
        self.add(payment_started_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        self.run_job()
        self.assertEqual(self.doc()["order_status"], "pending_payment")

    def test_razorpay_outage_does_not_break_the_job(self):
        self.use("captured"); self.add(); self.add("o2")
        self.rzp.fail = True
        self.run_job()
        self.assertEqual(self.doc()["order_status"], "pending_payment")

    def test_cod_and_session_less_orders_are_ignored(self):
        self.use("captured")
        self.add("cod", payment_mode="cod")
        self.add("nosession", razorpay_order_id=None)
        self.run_job()
        self.assertEqual(self.doc("cod")["order_status"], "pending_payment")
        self.assertEqual(self.doc("nosession")["order_status"], "pending_payment")


if __name__ == "__main__":
    unittest.main()