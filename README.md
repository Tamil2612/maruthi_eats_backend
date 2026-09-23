# Maruthi Eats — Python Backend

Cloud Functions for Python (2nd gen), built directly against your existing
`maruthi_eats` Firestore schema. Nothing here requires migrating off
Firestore — it adds server-side logic on top of the database you already
have.

## Project layout
```
backend/
  firebase.json          # points Firebase CLI at functions/, runtime = python312
  firestore.rules         # security rules (currently likely unset/default!)
  firestore.indexes.json  # composite indexes the reports screen + scheduled job need
  .firebaserc              # your project id goes here
  functions/
    main.py               # the 5 functions, see docstring at the top
    order_validation.py   # shared pricing/coupon logic
    requirements.txt
```

## 1. One-time setup

```bash
# Install the Firebase CLI if you don't have it
npm install -g firebase-tools

# Log in
firebase login

# From inside this backend/ folder:
firebase use --add          # pick your existing Firebase project
```

The included `.firebaserc` is already configured for `maruthi-eats`. If your Firebase project has a different ID, run `firebase use --add` and select the correct project.

## 2. Python environment (for local testing)

```bash
cd functions
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Get a service account key (only needed for local emulator testing)

Firebase Console → Project Settings → Service Accounts → Generate new
private key. Save it as `functions/service-account.json` (already covered
by a `.gitignore` entry you should add — **never commit this file**).

## 4. Set secrets (for Razorpay)

```bash
firebase functions:secrets:set RAZORPAY_KEY_ID
firebase functions:secrets:set RAZORPAY_KEY_SECRET
firebase functions:secrets:set RAZORPAY_WEBHOOK_SECRET
```
All three are read as runtime secrets by the backend. The Razorpay key ID is returned to the Flutter client by `razorpay_create_order`, so it is not a password, but keeping it in deployment configuration avoids hardcoding it in source.

## 5. Deploy

```bash
# From the backend/ folder (not functions/)
firebase deploy --only firestore:rules,firestore:indexes,functions
```

First deploy takes a few minutes. Watch for errors — a missing Razorpay secret
will prevent the payment functions from working correctly; the other functions can still deploy.

## 6. Point Razorpay's webhook at your function

After deploying, `firebase deploy` prints your function URLs. Take the
`razorpay_webhook` URL and add it in the Razorpay dashboard under
Settings → Webhooks, subscribing to `payment.captured` and
`payment.failed`. Use the same secret you set in step 4 as the webhook
secret in Razorpay's dashboard.

---

## What each function needs from the Flutter side

### `on_order_created` (audit trigger)
**No Flutter changes needed.** Deploy this first — it just watches orders
as they're created today and adds `validation_status`/`validation_notes`
fields. Optionally, surface `validation_status == 'flagged'` in the admin
app's `order_detail_screen.dart` so staff can spot suspicious orders.

### `place_order` (secure order creation)
Requires rewriting `_placeOrder` in `checkout_screen.dart` to call this
instead of writing to Firestore directly:

```dart
final callable = FirebaseFunctions.instanceFor(region: 'asia-south1')
    .httpsCallable('place_order');
final result = await callable.call({
  'items': cart.items.values.map((c) => c.toOrderMap()).toList(),
  'coupon_code': cart.appliedCoupon?.code,
  'payment_mode': _payment == PaymentChoice.upi ? 'upi' : 'cod',
  'delivery_address': selectedAddress.fullAddress,
  'address_label': selectedAddress.label,
  'latitude': selectedAddress.latitude,
  'longitude': selectedAddress.longitude,
});
final orderId = result.data['order_id'];
```
Add `cloud_functions: ^11.3.0` (or current version) to `pubspec.yaml`.
Wrap the call in try/catch for `FirebaseFunctionsException` and show
`e.message` to the user (it'll contain things like "Coupon has expired"
or "Item no longer available" instead of a generic error).
If you adopt this, also flip the Firestore rule for `orders` creation to
`allow create: if false` (already set that way in `firestore.rules`).

### `on_order_status_updated` (push notifications)
Requires, in the Flutter customer app:
1. Add `firebase_messaging` to `pubspec.yaml`.
2. In `auth_service.dart`, after login, save the device token:
   ```dart
   final token = await FirebaseMessaging.instance.getToken();
   if (token != null) {
     await _db.collection('users').doc(uid).update({'fcm_token': token});
   }
   FirebaseMessaging.instance.onTokenRefresh.listen((newToken) {
     _db.collection('users').doc(uid).update({'fcm_token': newToken});
   });
   ```
3. In `main.dart`, register a background handler and request notification
   permission (`FirebaseMessaging.instance.requestPermission()`).
4. iOS: upload an APNs auth key in Firebase Console → Project Settings →
   Cloud Messaging.

### `expire_promotions` (scheduled cleanup)
**No Flutter changes needed.** Runs automatically every day at 00:00 UTC.

### `razorpay_create_order` / `razorpay_webhook`
Requires, in `checkout_screen.dart`:
1. Add `razorpay_flutter` to `pubspec.yaml`.
2. Replace the `// TODO: if _payment == PaymentChoice.upi, ...` comment
   with: after creating the order (or right after `place_order` returns
   an `order_id`), call the `razorpay_create_order` callable, then open
   Razorpay's checkout using the returned `razorpay_order_id`/`key_id`:
   ```dart
   final session = await FirebaseFunctions.instance
       .httpsCallable('razorpay_create_order')
       .call({'order_id': orderId});
   final razorpay = Razorpay();
   razorpay.open({
     'key': session.data['key_id'],
     'amount': session.data['amount'],
     'order_id': session.data['razorpay_order_id'],
   });
   ```
3. Only navigate to `OrderSuccessScreen` from Razorpay's `onPaymentSuccess`
   callback — not immediately after creating the order. Handle
   `onPaymentError` by showing a retry option instead of silently
   succeeding (this is the actual fix for the "UPI orders succeed without
   paying" gap).
4. The webhook is the source of truth for `payment_status` — the
   client-side success callback is just for UX (letting the user proceed
   immediately); don't skip the webhook thinking the callback is enough,
   since it can be spoofed.

---

## Local testing with the emulator

```bash
firebase emulators:start --only functions,firestore
```
This runs everything locally against a fake Firestore, so you can hit
`place_order` etc. from a local build of the Flutter app pointed at the
emulator (`FirebaseFirestore.instance.useFirestoreEmulator(...)` and the
equivalent for Functions) before touching production data.

## Known gaps left for later
- `place_order` trusts the submitted price for combo/offer bundle items
  rather than re-deriving it from `offers/{id}` server-side — see the
  comment in `functions/order_validation.py`. Fine for now since offers are
  admin-curated, but worth tightening if combo abuse becomes a concern.
- No refund flow — `razorpay_webhook` only handles `payment.captured` and
  `payment.failed`. Add a `refund.processed` handler if you build
  cancellations-with-refund later.
