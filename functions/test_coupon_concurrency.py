import concurrent.futures
import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import google.cloud.firestore
import main
from firebase_functions import https_fn
from order_validation import validate_coupon


class CouponConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Determine if we are running against real Firestore Emulator or in-memory FakeDB
        cls.use_emulator = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
        if cls.use_emulator:
            print(f"[TEST SETUP] Running integration tests against Firestore Emulator at {os.environ.get('FIRESTORE_EMULATOR_HOST')}")
            cls.db = google.cloud.firestore.Client(project="maruthi-eats")
        else:
            print("[TEST SETUP] Running unit tests with in-memory FakeDB")
            from test_coupon_limits import FakeDB
            cls.db = FakeDB()

        main.firestore.client = lambda: cls.db

    def setUp(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        past = datetime.now(timezone.utc) - timedelta(days=1)

        # Helper to set up collection documents
        if self.use_emulator:
            # Clear test collections in emulator
            for col in ["orders", "coupons", "coupon_redemptions", "menu_items", "offers", "settings"]:
                docs = self.db.collection(col).list_documents()
                for doc in docs:
                    doc.delete()

            # Seed menu items
            self.db.collection("menu_items").document("A").set({"name": "Biryani", "price": 100, "available": True})
            self.db.collection("menu_items").document("B").set({"name": "Naan", "price": 50, "available": True})

            # Seed settings
            self.db.collection("settings").document("restaurant").set({
                "is_open": True,
                "timezone": "Asia/Kolkata",
                "minimum_order_value": 100.0,
                "delivery": {
                    "enabled": True,
                    "restaurant_latitude": 13.0827,
                    "restaurant_longitude": 80.2707,
                    "max_delivery_distance_km": 10.0,
                }
            })

            # Seed coupons
            self.db.collection("coupons").document("c_global10").set({
                "code": "GLOBAL10",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
                "max_global_uses": 10,
                "current_global_uses": 9,
            })
            self.db.collection("coupons").document("c_global_full").set({
                "code": "GLOBAL_FULL",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
                "max_global_uses": 10,
                "current_global_uses": 10,
            })
            self.db.collection("coupons").document("c_once").set({
                "code": "ONCE30",
                "amount": 30,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
                "max_uses_per_user": 1,
            })
            self.db.collection("coupons").document("c_first").set({
                "code": "FIRST50",
                "amount": 50,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
                "is_first_order_only": True,
            })
            self.db.collection("coupons").document("c_cod").set({
                "code": "COD20",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
            })
            self.db.collection("coupons").document("c_online").set({
                "code": "ONLINE20",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
            })
            self.db.collection("coupons").document("c_cancel").set({
                "code": "CANCEL20",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": future,
                "max_global_uses": 5,
                "current_global_uses": 0,
            })
            self.db.collection("coupons").document("c_inactive").set({
                "code": "INACTIVE",
                "amount": 20,
                "min_order_value": 100,
                "is_active": False,
                "expiry_date": future,
            })
            self.db.collection("coupons").document("c_expired").set({
                "code": "EXPIRED",
                "amount": 20,
                "min_order_value": 100,
                "is_active": True,
                "expiry_date": past,
            })
        else:
            from test_coupon_limits import FakeDB
            self.db = FakeDB()
            main.firestore.client = lambda: self.db
            self.db.data["coupons"].update({
                "c_global10": {
                    "code": "GLOBAL10",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_global_uses": 10,
                    "current_global_uses": 9,
                },
                "c_global_full": {
                    "code": "GLOBAL_FULL",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_global_uses": 10,
                    "current_global_uses": 10,
                },
                "c_once": {
                    "code": "ONCE30",
                    "amount": 30,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_uses_per_user": 1,
                },
                "c_first": {
                    "code": "FIRST50",
                    "amount": 50,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "is_first_order_only": True,
                },
                "c_cod": {
                    "code": "COD20",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                },
                "c_online": {
                    "code": "ONLINE20",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                },
                "c_cancel": {
                    "code": "CANCEL20",
                    "amount": 20,
                    "min_order_value": 100,
                    "is_active": True,
                    "expiry_date": future,
                    "max_global_uses": 5,
                    "current_global_uses": 0,
                },
            })

    def place(self, items, uid="u_test", coupon_code=None, payment_mode="cod", idempotency_key=None):
        data = {
            "items": items,
            "payment_mode": payment_mode,
            "delivery_address": "123 Main St",
            "latitude": 13.0827,
            "longitude": 80.2707,
            "coupon_code": coupon_code,
        }
        if idempotency_key:
            data["idempotency_key"] = idempotency_key

        req = SimpleNamespace(auth=SimpleNamespace(uid=uid), data=data)
        out = inspect.unwrap(main.place_order)(req)

        if self.use_emulator:
            order_doc = self.db.collection("orders").document(out["order_id"]).get()
            order_dict = order_doc.to_dict()
        else:
            order_dict = self.db.data["orders"][out["order_id"]]

        return out, order_dict

    def _line(self, item_id="A", qty=2, price=100):
        return {"item_id": item_id, "name": "Biryani", "price": price, "qty": qty}

    # 1. Global limit: 1 remaining use with simultaneous checkout requests
    def test_1_global_limit_one_remaining_concurrent_checkout(self):
        uids = ["u_glob_1", "u_glob_2"]
        results = []
        errors = []

        def task(uid):
            try:
                out, order = self.place([self._line()], uid=uid, coupon_code="GLOBAL10")
                results.append((uid, out["order_id"]))
            except https_fn.HttpsError as e:
                errors.append((uid, e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(task, uid) for uid in uids]
            concurrent.futures.wait(futures)

        self.assertEqual(len(results), 1, f"Expected exactly 1 success, got {len(results)}")
        self.assertEqual(len(errors), 1, f"Expected exactly 1 error, got {len(errors)}")
        self.assertIn("global usage limit", str(errors[0][1].message))

        # Check global counter in database
        if self.use_emulator:
            c_doc = self.db.collection("coupons").document("c_global10").get().to_dict()
        else:
            c_doc = self.db.data["coupons"]["c_global10"]
        self.assertEqual(c_doc["current_global_uses"], 10)

    # 2. Global limit already reached
    def test_2_global_limit_already_reached(self):
        with self.assertRaises(https_fn.HttpsError) as cm:
            self.place([self._line()], uid="u_full", coupon_code="GLOBAL_FULL")
        self.assertIn("global usage limit", str(cm.exception.message))

    # 3. Per-user limit: simultaneous requests by the same user
    def test_3_per_user_limit_concurrent_checkout(self):
        uids = ["u_user_limit", "u_user_limit"]
        results = []
        errors = []

        def task(uid):
            try:
                out, order = self.place([self._line()], uid=uid, coupon_code="ONCE30")
                results.append((uid, out["order_id"]))
            except https_fn.HttpsError as e:
                errors.append((uid, e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(task, uid) for uid in uids]
            concurrent.futures.wait(futures)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("already used", str(errors[0][1].message))

    # 4. Simultaneous first orders by the same user for a first-order-only coupon
    def test_4_first_order_only_concurrent_checkout(self):
        uids = ["u_first_time", "u_first_time"]
        results = []
        errors = []

        def task(uid):
            try:
                out, order = self.place([self._line()], uid=uid, coupon_code="FIRST50")
                results.append((uid, out["order_id"]))
            except https_fn.HttpsError as e:
                errors.append((uid, e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(task, uid) for uid in uids]
            concurrent.futures.wait(futures)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("first order only", str(errors[0][1].message))

    # 5. Successful COD order creation and redemption
    def test_5_successful_cod_order_creation_and_redemption(self):
        out, order = self.place([self._line()], uid="u_cod", coupon_code="COD20", payment_mode="cod")
        self.assertEqual(order["order_status"], "placed")
        self.assertEqual(order["coupon_discount"], 20.0)

        # Check redemption doc
        if self.use_emulator:
            red_doc = self.db.collection("coupon_redemptions").document(f"c_cod_{out['order_id']}").get().to_dict()
        else:
            red_doc = self.db.data["coupon_redemptions"].get(f"c_cod_{out['order_id']}")
        self.assertIsNotNone(red_doc)
        self.assertEqual(red_doc["status"], "completed")

    # 6. Successful online order reservation followed by payment confirmation
    def test_6_online_order_reservation_and_payment_confirmation(self):
        out, order = self.place([self._line()], uid="u_online", coupon_code="ONLINE20", payment_mode="upi")
        order_id = out["order_id"]
        self.assertEqual(order["order_status"], "pending_payment")

        # Check redemption doc is reserved
        if self.use_emulator:
            red_ref = self.db.collection("coupon_redemptions").document(f"c_online_{order_id}")
            red_doc = red_ref.get().to_dict()
        else:
            red_ref = self.db.collection("coupon_redemptions").document(f"c_online_{order_id}")
            red_doc = self.db.data["coupon_redemptions"].get(f"c_online_{order_id}")

        self.assertEqual(red_doc["status"], "reserved")

        # Mark paid
        order_ref = self.db.collection("orders").document(order_id)
        res = main._mark_paid_txn(self.db.transaction(), order_ref, "pay_test_6")
        self.assertEqual(res, "paid")

        # Check redemption status updated to completed
        if self.use_emulator:
            updated_red = red_ref.get().to_dict()
        else:
            updated_red = self.db.data["coupon_redemptions"].get(f"c_online_{order_id}")
        self.assertEqual(updated_red["status"], "completed")

    # 7. Payment failure / cancellation releases reservation exactly once
    def test_7_cancellation_releases_reservation_once(self):
        out, order = self.place([self._line()], uid="u_cancel", coupon_code="CANCEL20", payment_mode="cod")
        order_id = out["order_id"]
        order_ref = self.db.collection("orders").document(order_id)

        # Cancel order
        main._change_status_txn(self.db.transaction(), order_ref, "cancelled", "u_cancel", False, "User cancelled")

        # Verify redemption released
        if self.use_emulator:
            c_doc = self.db.collection("coupons").document("c_cancel").get().to_dict()
            red_doc = self.db.collection("coupon_redemptions").document(f"c_cancel_{order_id}").get().to_dict()
        else:
            c_doc = self.db.data["coupons"]["c_cancel"]
            red_doc = self.db.data["coupon_redemptions"][f"c_cancel_{order_id}"]

        self.assertEqual(red_doc["status"], "released")
        self.assertEqual(c_doc["current_global_uses"], 0)

        # Release again (duplicate call)
        order_snap = order_ref.get().to_dict()
        main._release_coupon_redemption_txn(self.db, self.db.transaction(), order_ref, order_snap)

        # Verify usage count not decremented again
        if self.use_emulator:
            c_doc2 = self.db.collection("coupons").document("c_cancel").get().to_dict()
        else:
            c_doc2 = self.db.data["coupons"]["c_cancel"]
        self.assertEqual(c_doc2["current_global_uses"], 0)

    # 8. Duplicate payment webhooks / callbacks
    def test_8_duplicate_payment_webhooks(self):
        out, order = self.place([self._line()], uid="u_dup", coupon_code="ONLINE20", payment_mode="upi")
        order_id = out["order_id"]
        order_ref = self.db.collection("orders").document(order_id)

        # 1st mark paid
        res1 = main._mark_paid_txn(self.db.transaction(), order_ref, "pay_dup_123")
        self.assertEqual(res1, "paid")

        # 2nd mark paid (duplicate webhook)
        res2 = main._mark_paid_txn(self.db.transaction(), order_ref, "pay_dup_123")
        self.assertEqual(res2, "already_paid")

    # 9. Repeated place_order requests and idempotency
    def test_9_repeated_place_order_idempotency(self):
        out1, order1 = self.place([self._line()], uid="u_idem", coupon_code="COD20", idempotency_key="key_123")
        out2, order2 = self.place([self._line()], uid="u_idem", coupon_code="COD20", idempotency_key="key_123")

        self.assertEqual(out1["order_id"], out2["order_id"])
        self.assertEqual(out1["total"], out2["total"])

    # 10. Order without coupon
    def test_10_order_without_coupon(self):
        out, order = self.place([self._line()], uid="u_no_coupon", coupon_code=None)
        self.assertTrue(out["order_id"])
        self.assertEqual(order["coupon_discount"], 0.0)

    # 11. Invalid, expired, inactive, and minimum-order-value failures
    def test_11_invalid_expired_inactive_and_min_order_failures(self):
        # Invalid code
        with self.assertRaises(https_fn.HttpsError) as cm1:
            self.place([self._line()], uid="u11", coupon_code="NONEXISTENT")
        self.assertIn("does not exist", str(cm1.exception.message))

        # Inactive code
        with self.assertRaises(https_fn.HttpsError) as cm2:
            self.place([self._line()], uid="u11", coupon_code="INACTIVE")
        self.assertIn("not active", str(cm2.exception.message))

        # Expired code
        with self.assertRaises(https_fn.HttpsError) as cm3:
            self.place([self._line()], uid="u11", coupon_code="EXPIRED")
        self.assertIn("expired", str(cm3.exception.message))

        # Minimum order value failure
        amt, err = validate_coupon(self.db, "FIRST50", 50, user_id="u11")
        self.assertIsNotNone(err)
        self.assertIn("minimum order", err)

    # 12. Transaction retries without duplicate orders, redemptions, or counter changes
    def test_12_transaction_retries_consistency(self):
        # Test that _create_order_and_reserve_coupon_txn handles execution retries safely
        order_ref = self.db.collection("orders").document("idem_retry_test")
        order_data = {
            "customer_id": "u_retry",
            "items": [self._line()],
            "delivery_fee": 30.0,
            "payment_mode": "cod",
        }

        # First attempt
        total1 = main._create_order_and_reserve_coupon_txn(
            self.db.transaction(),
            self.db,
            order_ref,
            order_data,
            "COD20",
            100,
            "u_retry",
            "cod",
        )

        # Retry attempt on same order_ref
        total2 = main._create_order_and_reserve_coupon_txn(
            self.db.transaction(),
            self.db,
            order_ref,
            order_data,
            "COD20",
            100,
            "u_retry",
            "cod",
        )

        self.assertEqual(total1, total2)


if __name__ == "__main__":
    unittest.main()
