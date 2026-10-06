"""Offline tests for the refund flow (no Firebase / Razorpay needed):
    python -m unittest test_refunds -v        (run inside functions/, with the venv)
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

main._mark_paid_txn = getattr(main._mark_paid_txn, "__wrapped__", main._mark_paid_txn)
main._claim_refund_txn = getattr(main._claim_refund_txn, "__wrapped__", main._claim_refund_txn)
main._expire_txn = getattr(main._expire_txn, "__wrapped__", main._expire_txn)
main._fail_stuck_txn = getattr(main._fail_stuck_txn, "__wrapped__", main._fail_stuck_txn)

DELETE = _fs.DELETE_FIELD
SERVER_TS = _fs.SERVER_TIMESTAMP


class FakeSnap:
    def __init__(self, id, data):
        self.id, self._d, self.exists = id, data, data is not None
        self.reference = None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


class FakeRef:
    def __init__(self, db, coll, id):
        self.db, self.coll, self.id = db, coll, id

    def get(self, transaction=None):
        return FakeSnap(self.id, self.db.data.get(self.coll, {}).get(self.id))

    def update(self, changes):
        doc = self.db.data[self.coll][self.id]
        for k, v in changes.items():
            if v is DELETE:
                doc.pop(k, None)
            elif v is SERVER_TS:
                doc[k] = "<ts>"
            else:
                doc[k] = v


class FakeTxn:
    _read_only = False
    _max_attempts = 1
    _rollback = lambda self: None
    _clean_up = lambda self: None
    _begin = lambda self, **kw: None
    _commit = lambda self: None
    _id = b"fake_txn_id"

    def update(self, ref, changes):
        ref.update(changes)


class FakeQuery:
    def __init__(self, db, coll, rows):
        self.db, self.coll, self.rows = db, coll, rows

    def limit(self, n):
        return FakeQuery(self.db, self.coll, self.rows[:n])

    def get(self):
        out = []
        for i in self.rows:
            snap = FakeSnap(i, self.db.data[self.coll][i])
            snap.reference = FakeRef(self.db, self.coll, i)
            out.append(snap)
        return out


class FakeCol:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, id):
        return FakeRef(self.db, self.name, id)

    def where(self, field, op, value):
        rows = [i for i, d in self.db.data.get(self.name, {}).items() if d.get(field) == value]
        return FakeQuery(self.db, self.name, rows)


class FakeDB:
    def __init__(self):
        self.data = {
            "users": {"cust": {"fcm_token": "tok"}},
            "orders": {},
        }

    def collection(self, name):
        return FakeCol(self, name)

    def transaction(self):
        return FakeTxn()


class FakeRazorpay:
    """Stands in for razorpay.Client."""

    def __init__(self, payment, refund_status="processed", fail=False):
        self.payment_data = payment
        self.refund_status = refund_status
        self.fail = fail
        self.refund_calls = []
        self.payment = SimpleNamespace(fetch=self._fetch, refund=self._refund)

    def _fetch(self, payment_id):
        return dict(self.payment_data)

    def _refund(self, payment_id, data):
        if self.fail:
            raise RuntimeError("gateway down")
        self.refund_calls.append((payment_id, data))
        self.payment_data["amount_refunded"] = self.payment_data.get("amount_refunded", 0) + data["amount"]
        return {"id": "rfnd_1", "status": self.refund_status, "amount": data["amount"]}


def paid_order(**kw):
    base = {"customer_id": "cust", "payment_mode": "upi", "payment_status": "paid",
            "order_status": "placed", "razorpay_payment_id": "pay_1", "razorpay_amount": 27000}
    base.update(kw)
    return base


def event(before, after, ref):
    return SimpleNamespace(
        data=SimpleNamespace(before=SimpleNamespace(to_dict=lambda: before),
                             after=SimpleNamespace(to_dict=lambda: after, reference=ref)),
        params={"order_id": ref.id})


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.pushes = []
        main.firestore.client = lambda: self.db
        main._send_customer_push = lambda cid, body, data: self.pushes.append((cid, body))
        self.admin_alerts = []
        main._notify_admin_refund_failed = lambda oid: self.admin_alerts.append(oid)
        self.rzp = FakeRazorpay({"status": "captured", "amount": 27000, "amount_refunded": 0})
        main._razorpay_client = lambda: (self.rzp, "rzp_test_x")

    def order(self, oid="o1", **kw):
        self.db.data["orders"][oid] = paid_order(**kw)
        return FakeRef(self.db, "orders", oid)

    def cancel(self, ref):
        before = dict(self.db.data["orders"][ref.id])
        self.db.data["orders"][ref.id]["order_status"] = "cancelled"
        after = dict(self.db.data["orders"][ref.id])
        inspect.unwrap(main.on_order_refund_needed)(event(before, after, ref))

    def doc(self, oid="o1"):
        return self.db.data["orders"][oid]

    # ---- the happy paths -----------------------------------------------------
    def test_cancel_paid_upi_refunds_in_full_and_notifies(self):
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(len(self.rzp.refund_calls), 1)
        pid, data = self.rzp.refund_calls[0]
        self.assertEqual((pid, data["amount"], data["speed"]), ("pay_1", 27000, "optimum"))
        self.assertEqual(self.doc()["refund_status"], "processed")
        self.assertEqual(self.doc()["refund_amount"], 270.0)
        self.assertEqual(self.doc()["refund_reason"], "order_cancelled")
        self.assertEqual(self.pushes, [("cust", "Your refund of ₹270 has been processed")])

    def test_pending_then_webhook_processed_notifies_once(self):
        self.rzp.refund_status = "pending"
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(self.doc()["refund_status"], "pending")
        self.assertEqual(self.doc()["refund_id"], "rfnd_1")
        self.assertEqual(self.pushes, [])
        entity = {"id": "rfnd_1", "payment_id": "pay_1", "amount": 27000}
        main._handle_refund_webhook(self.db, "refund.processed", entity, "pay_1")
        main._handle_refund_webhook(self.db, "refund.processed", entity, "pay_1")  # duplicate delivery
        self.assertEqual(self.doc()["refund_status"], "processed")
        self.assertEqual(len(self.pushes), 1)

    def test_late_payment_is_refunded(self):
        ref = self.order(payment_status="paid_needs_refund", order_status="payment_expired")
        before = dict(self.doc(), payment_status="paid")
        inspect.unwrap(main.on_order_refund_needed)(event(before, self.doc(), ref))
        self.assertEqual(self.doc()["refund_status"], "processed")
        self.assertEqual(self.doc()["refund_reason"], "late_payment")

    # ---- safety ----------------------------------------------------------------
    def test_trigger_redelivery_never_refunds_twice(self):
        ref = self.order()
        self.cancel(ref)
        before = dict(self.doc(), order_status="placed")
        inspect.unwrap(main.on_order_refund_needed)(event(before, self.doc(), ref))   # same event again
        self.assertEqual(len(self.rzp.refund_calls), 1)

    def test_cod_and_unpaid_orders_are_not_refunded(self):
        cod = self.order("cod", payment_mode="cod", payment_status="cod_pending", razorpay_payment_id=None)
        self.cancel(cod)
        unpaid = self.order("unpaid", payment_status="pending", razorpay_payment_id=None)
        self.cancel(unpaid)
        self.assertEqual(self.rzp.refund_calls, [])
        self.assertNotIn("refund_status", self.doc("cod"))
        self.assertNotIn("refund_status", self.doc("unpaid"))

    def test_already_refunded_at_razorpay_does_not_refund_again(self):
        self.rzp.payment_data.update(amount_refunded=27000, status="refunded")
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(self.rzp.refund_calls, [])
        self.assertEqual(self.doc()["refund_status"], "processed")

    def test_partial_remainder_only(self):
        self.rzp.payment_data["amount_refunded"] = 5000
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(self.rzp.refund_calls[0][1]["amount"], 22000)

    # ---- failures and retry -----------------------------------------------------
    def test_failure_is_recorded_and_retry_works(self):
        self.rzp.fail = True
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(self.doc()["refund_status"], "failed")
        self.assertIn("gateway down", self.doc()["refund_error"])
        self.assertEqual(self.admin_alerts, ["o1"])          # staff are told

        self.rzp.fail = False
        before = dict(self.doc())
        self.doc()["refund_status"] = "retry_requested"      # what the admin's Retry button writes
        inspect.unwrap(main.on_order_refund_needed)(event(before, dict(self.doc()), ref))
        self.assertEqual(self.doc()["refund_status"], "processed")
        self.assertNotIn("refund_error", self.doc())
        self.assertEqual(len(self.rzp.refund_calls), 1)

    def test_uncaptured_payment_fails_cleanly(self):
        self.rzp.payment_data["status"] = "authorized"
        ref = self.order()
        self.cancel(ref)
        self.assertEqual(self.doc()["refund_status"], "failed")
        self.assertEqual(self.rzp.refund_calls, [])

    def test_missing_payment_id_fails_without_calling_razorpay(self):
        ref = self.order(razorpay_payment_id=None)
        self.cancel(ref)
        self.assertEqual(self.doc()["refund_status"], "failed")
        self.assertEqual(self.rzp.refund_calls, [])
        self.assertEqual(self.admin_alerts, ["o1"])

    def test_refund_failed_webhook(self):
        self.rzp.refund_status = "pending"
        ref = self.order()
        self.cancel(ref)
        main._handle_refund_webhook(self.db, "refund.failed", {"id": "rfnd_1", "payment_id": "pay_1"}, "pay_1")
        self.assertEqual(self.doc()["refund_status"], "failed")

    def test_failed_webhook_never_undoes_a_processed_refund(self):
        ref = self.order()
        self.cancel(ref)
        main._handle_refund_webhook(self.db, "refund.failed", {"id": "rfnd_1", "payment_id": "pay_1"}, "pay_1")
        self.assertEqual(self.doc()["refund_status"], "processed")

    # ---- sweeper ---------------------------------------------------------------
    def test_sweeper_only_fails_refunds_that_never_reached_razorpay(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        self.order("stuck", refund_status="pending", refund_requested_at=old)
        self.order("waiting", refund_status="pending", refund_requested_at=old, refund_id="rfnd_9")
        self.order("fresh", refund_status="pending",
                   refund_requested_at=datetime.now(timezone.utc))
        inspect.unwrap(main.fail_stuck_refunds)(None)
        self.assertEqual(self.doc("stuck")["refund_status"], "failed")
        self.assertEqual(self.admin_alerts, ["stuck"])
        self.assertEqual(self.doc("waiting")["refund_status"], "pending")
        self.assertEqual(self.doc("fresh")["refund_status"], "pending")


if __name__ == "__main__":
    unittest.main()