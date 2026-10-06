"""
Restaurant Business-Rules Engine & Settings Helpers.

Provides server-authoritative logic for:
  - Restaurant open/closed/paused/special-closure availability
  - Timezone-aware opening hours (including overnight ranges)
  - Haversine distance calculation and delivery radius enforcement
  - Dynamic distance-based delivery fee slab calculation
  - Food subtotal minimum order value validation
"""

import math
from datetime import datetime, timezone
try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # Python < 3.9 compatibility fallback

from google.cloud.firestore import Client as FirestoreClient
from order_validation import parse_int, parse_float

# Default restaurant configuration (fallback if settings/restaurant is missing in Firestore)
DEFAULT_RESTAURANT_SETTINGS = {
    "is_open": True,
    "timezone": "Asia/Kolkata",
    "minimum_order_value": 150.0,
    "pause": {
        "is_paused": False,
        "paused_until": None,
        "reason": "",
    },
    "opening_hours": {
        "monday": {"enabled": True, "open": "10:00", "close": "22:30"},
        "tuesday": {"enabled": True, "open": "10:00", "close": "22:30"},
        "wednesday": {"enabled": True, "open": "10:00", "close": "22:30"},
        "thursday": {"enabled": True, "open": "10:00", "close": "22:30"},
        "friday": {"enabled": True, "open": "10:00", "close": "23:00"},
        "saturday": {"enabled": True, "open": "10:00", "close": "23:00"},
        "sunday": {"enabled": True, "open": "10:00", "close": "22:30"},
    },
    "special_closures": [],
    "delivery": {
        "enabled": True,
        "restaurant_latitude": 13.0827,  # Default Chennai coordinates
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


def load_restaurant_settings(db: FirestoreClient) -> dict:
    """
    Loads settings/restaurant from Firestore. If document is missing, returns
    DEFAULT_RESTAURANT_SETTINGS merged safely so the restaurant is not closed.
    """
    try:
        doc = db.collection("settings").document("restaurant").get()
        if doc.exists:
            data = doc.to_dict() or {}
            # Merge top-level keys with defaults
            merged = {**DEFAULT_RESTAURANT_SETTINGS, **data}
            # Deep merge nested dicts for safety
            if "pause" in data and isinstance(data["pause"], dict):
                merged["pause"] = {**DEFAULT_RESTAURANT_SETTINGS["pause"], **data["pause"]}
            if "opening_hours" in data and isinstance(data["opening_hours"], dict):
                merged["opening_hours"] = {**DEFAULT_RESTAURANT_SETTINGS["opening_hours"], **data["opening_hours"]}
            if "delivery" in data and isinstance(data["delivery"], dict):
                deliv = {**DEFAULT_RESTAURANT_SETTINGS["delivery"], **data["delivery"]}
                if "pricing" in data["delivery"] and isinstance(data["delivery"]["pricing"], dict):
                    deliv["pricing"] = {**DEFAULT_RESTAURANT_SETTINGS["delivery"]["pricing"], **data["delivery"]["pricing"]}
                merged["delivery"] = deliv
            return merged
    except Exception as e:
        print(f"[RESTAURANT SETTINGS] Error loading settings doc: {e}. Using defaults.")

    return dict(DEFAULT_RESTAURANT_SETTINGS)


def get_restaurant_tz(tz_name: str = "Asia/Kolkata") -> zoneinfo.ZoneInfo:
    """Returns zoneinfo.ZoneInfo for tz_name, defaulting to Asia/Kolkata."""
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return zoneinfo.ZoneInfo("Asia/Kolkata")


def get_restaurant_local_time(tz_name: str = "Asia/Kolkata", now_utc: datetime | None = None) -> datetime:
    """
    Returns current time in the restaurant's configured local timezone.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    tz = get_restaurant_tz(tz_name)
    return now_utc.astimezone(tz)


def is_temporarily_paused(pause_settings: dict | None, now_utc: datetime | None = None) -> tuple[bool, str]:
    """
    Checks if pause.is_paused is True and paused_until is still in the future.
    If paused_until has passed, the pause is automatically considered expired.
    Returns (is_paused, reason).
    """
    if not pause_settings or not isinstance(pause_settings, dict):
        return False, ""

    if not pause_settings.get("is_paused", False):
        return False, ""

    paused_until = pause_settings.get("paused_until")
    reason = str(pause_settings.get("reason") or "Restaurant is temporarily paused").strip()

    if paused_until is None:
        # Paused indefinitely until manually resumed
        return True, reason or "Restaurant is temporarily paused"

    # Parse paused_until if string or Firestore Timestamp
    if isinstance(paused_until, str):
        try:
            paused_until = datetime.fromisoformat(paused_until)
        except ValueError:
            return True, reason

    if isinstance(paused_until, datetime):
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=timezone.utc)

        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        elif now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        if now_utc < paused_until:
            return True, reason or "Restaurant is temporarily paused"

    # If paused_until has passed, automatically consider it available
    return False, ""


def is_special_closure(special_closures: list | None, local_date_str: str) -> tuple[bool, str]:
    """
    Checks if local_date_str ("YYYY-MM-DD") matches an active entry in special_closures.
    Returns (is_closed, reason).
    """
    if not special_closures or not isinstance(special_closures, list):
        return False, ""

    for sc in special_closures:
        if not isinstance(sc, dict):
            continue
        sc_date = str(sc.get("date") or "").strip()
        sc_closed = bool(sc.get("closed", True))
        sc_reason = str(sc.get("reason") or "Special Holiday").strip()

        if sc_date == local_date_str and sc_closed:
            return True, sc_reason

    return False, ""


def _parse_time_minutes(time_str: str) -> int:
    """Parses 'HH:MM' into minutes since midnight."""
    try:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 0


def format_12h_time(time_str: str) -> str:
    """Converts '22:30' to '10:30 PM' or '10:00' to '10:00 AM'."""
    try:
        mins = _parse_time_minutes(time_str)
        hours = mins // 60
        m = mins % 60
        suffix = "AM" if hours < 12 else "PM"
        h12 = hours % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return time_str


def is_restaurant_open_hours(opening_hours: dict | None, local_dt: datetime) -> tuple[bool, str]:
    """
    Checks if local_dt falls within the configured opening hours for that day of week.
    Supports overnight ranges e.g. 18:00 to 02:00.
    Returns (is_open, next_open_message).
    """
    if not opening_hours or not isinstance(opening_hours, dict):
        return True, ""

    day_name = local_dt.strftime("%A").lower()  # monday, tuesday, etc.
    day_config = opening_hours.get(day_name) or {}

    if not day_config.get("enabled", True):
        return False, f"Closed on {local_dt.strftime('%A')}s"

    open_str = str(day_config.get("open") or "10:00").strip()
    close_str = str(day_config.get("close") or "22:30").strip()

    open_mins = _parse_time_minutes(open_str)
    close_mins = _parse_time_minutes(close_str)
    now_mins = local_dt.hour * 60 + local_dt.minute

    # Overnight range (e.g. 18:00 to 02:00 -> open_mins > close_mins)
    if open_mins > close_mins:
        is_open = now_mins >= open_mins or now_mins < close_mins
    else:
        is_open = open_mins <= now_mins < close_mins

    if is_open:
        return True, ""

    # Formulate next opening message
    open_12h = format_12h_time(open_str)
    if now_mins < open_mins:
        msg = f"Opens today at {open_12h}"
    else:
        msg = f"Opens at {open_12h}"

    return False, msg


def check_restaurant_availability(settings: dict, now_utc: datetime | None = None) -> tuple[bool, str, str]:
    """
    Enforces all 5 open/closed business rules:
      1. is_open == True
      2. delivery.enabled == True
      3. Not a special closure
      4. Within opening hours (using restaurant timezone)
      5. Temporary pause is not active

    Returns (is_available, status_code, user_message).
    status_code can be: 'OPEN', 'MANUALLY_CLOSED', 'DELIVERY_DISABLED', 'PAUSED', 'SPECIAL_CLOSURE', 'OUTSIDE_HOURS'
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Rule 1: Manual Master Switch
    if not settings.get("is_open", True):
        return False, "MANUALLY_CLOSED", "Restaurant is currently closed."

    # Rule 2: Delivery Enabled
    delivery = settings.get("delivery") or {}
    if not delivery.get("enabled", True):
        return False, "DELIVERY_DISABLED", "Delivery is currently disabled."

    # Rule 3: Temporary Pause
    paused, pause_reason = is_temporarily_paused(settings.get("pause"), now_utc)
    if paused:
        return False, "PAUSED", f"Restaurant is temporarily paused: {pause_reason}"

    # Rule 4 & 5: Timezone-aware Special Closures & Opening Hours
    tz_name = str(settings.get("timezone") or "Asia/Kolkata")
    local_dt = get_restaurant_local_time(tz_name, now_utc)
    local_date_str = local_dt.strftime("%Y-%m-%d")

    closed_today, closure_reason = is_special_closure(settings.get("special_closures"), local_date_str)
    if closed_today:
        return False, "SPECIAL_CLOSURE", f"Closed today — {closure_reason}"

    in_hours, hours_msg = is_restaurant_open_hours(settings.get("opening_hours"), local_dt)
    if not in_hours:
        return False, "OUTSIDE_HOURS", f"Restaurant is currently closed. {hours_msg}"

    return True, "OPEN", "Open — Accepting orders"


def calculate_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculates straight-line distance between two coordinates in kilometers
    using the Haversine formula. Rounded to 2 decimal places.
    """
    R = 6371.0  # Earth radius in kilometers

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2) + \
        (math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    distance = R * c
    return round(distance, 2)


def validate_delivery_distance(delivery_settings: dict, distance_km: float) -> tuple[bool, str]:
    """
    Validates that distance_km is within max_delivery_distance_km.
    """
    max_dist = parse_float(delivery_settings.get("max_delivery_distance_km"), default=8.0)
    if distance_km > max_dist:
        return False, f"Sorry, this address is outside our delivery area ({distance_km} km away, max is {max_dist} km)."
    return True, ""


def calculate_delivery_fee(delivery_settings: dict, distance_km: float) -> float:
    """
    Calculates delivery fee dynamically based on sorted slabs from delivery.pricing.slabs.
    Slab format: [{"up_to_km": 3, "fee": 30}, {"up_to_km": 5, "fee": 40}, {"up_to_km": 8, "fee": 60}]
    """
    pricing = delivery_settings.get("pricing") or {}
    slabs = pricing.get("slabs") or [
        {"up_to_km": 3.0, "fee": 30.0},
        {"up_to_km": 5.0, "fee": 40.0},
        {"up_to_km": 8.0, "fee": 60.0},
    ]

    # Sort slabs by up_to_km ascending
    sorted_slabs = sorted(slabs, key=lambda s: parse_float(s.get("up_to_km"), default=999.0))

    for slab in sorted_slabs:
        up_to = parse_float(slab.get("up_to_km"), default=999.0)
        fee = parse_float(slab.get("fee"), default=30.0)
        if distance_km <= up_to:
            return fee

    # Fallback to last slab fee if beyond all slabs
    if sorted_slabs:
        return parse_float(sorted_slabs[-1].get("fee"), default=60.0)

    return 30.0


def validate_minimum_order(settings: dict, food_subtotal: float) -> tuple[bool, str]:
    """
    Validates that food_subtotal (BEFORE coupon discount and EXCLUDING delivery fee)
    meets minimum_order_value.
    """
    min_val = parse_float(settings.get("minimum_order_value"), default=150.0)
    if food_subtotal < min_val:
        shortfall = round(min_val - food_subtotal, 2)
        return False, f"Minimum order value is ₹{int(min_val)}. Add ₹{int(shortfall) if shortfall == int(shortfall) else shortfall} more to place order."
    return True, ""
