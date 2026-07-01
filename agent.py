import os
import re
import json
import math
import smtplib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


# ============================================================
# Config
# ============================================================

load_dotenv()

NS_URL = os.environ["NIGHTSCOUT_URL"].rstrip("/")
NS_TOKEN = os.environ.get("NIGHTSCOUT_TOKEN", "").strip()

EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
EMAIL_USER = os.environ["EMAIL_USER"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]

JULIE_EMAIL = os.environ["JULIE_EMAIL"]
PARENT_EMAIL = os.environ["PARENT_EMAIL"]

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() in {"1", "true", "yes", "y"}
LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))

STATE_FILE = Path("alert_state.json")
PODAGE_FILE = Path("podage.txt")
DEXCOMAGE_FILE = Path("dexcomage.txt")


# ============================================================
# Thresholds
# ============================================================

LOW_NOW = float(os.environ.get("LOW_NOW", "70"))
LOW_SOON_30_MIN = float(os.environ.get("LOW_SOON_30_MIN", "75"))
LOW_SOON_60_MIN = float(os.environ.get("LOW_SOON_60_MIN", "70"))

HIGH_NOW = float(os.environ.get("HIGH_NOW", "250"))
HIGH_PREDICTED = float(os.environ.get("HIGH_PREDICTED", "220"))

STALE_MINUTES = float(os.environ.get("STALE_MINUTES", "15"))

# Pod age / Omnipod age
POD_WARN_HOURS = float(os.environ.get("POD_WARN_HOURS", "68"))
POD_EXPIRE_HOURS = float(os.environ.get("POD_EXPIRE_HOURS", "72"))
POD_RECORD_STALE_HOURS = float(os.environ.get("POD_RECORD_STALE_HOURS", "84"))

# Dexcom G7 age
DEXCOM_WARN_HOURS = float(os.environ.get("DEXCOM_WARN_HOURS", str(9 * 24 + 18)))     # 9d18h
DEXCOM_REPLACE_HOURS = float(os.environ.get("DEXCOM_REPLACE_HOURS", str(10 * 24)))   # 10d
DEXCOM_URGENT_HOURS = float(os.environ.get("DEXCOM_URGENT_HOURS", str(10 * 24 + 10))) # 10d10h
DEXCOM_RECORD_STALE_HOURS = float(os.environ.get("DEXCOM_RECORD_STALE_HOURS", "264")) # 11d

# Pump insulin estimation
POD_INITIAL_UNITS = float(os.environ.get("POD_INITIAL_UNITS", "150"))
POD_INSULIN_WARN_U = float(os.environ.get("POD_INSULIN_WARN_U", "10"))
POD_INSULIN_URGENT_U = float(os.environ.get("POD_INSULIN_URGENT_U", "5"))

# Missing bolus logic
MISSING_BOLUS_LOOKBACK_MIN = float(os.environ.get("MISSING_BOLUS_LOOKBACK_MIN", "60"))
MISSING_BOLUS_MIN_CURRENT_BG = float(os.environ.get("MISSING_BOLUS_MIN_CURRENT_BG", "150"))
MISSING_BOLUS_MIN_RISE = float(os.environ.get("MISSING_BOLUS_MIN_RISE", "40"))
MISSING_BOLUS_MIN_SLOPE = float(os.environ.get("MISSING_BOLUS_MIN_SLOPE", "0.45"))
MISSING_BOLUS_MIN_DURATION = float(os.environ.get("MISSING_BOLUS_MIN_DURATION", "35"))
MISSING_BOLUS_MIN_POINTS = int(os.environ.get("MISSING_BOLUS_MIN_POINTS", "6"))


# ============================================================
# JSON state
# ============================================================

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def load_alert_state():
    return load_json(STATE_FILE, {})


def save_alert_state(state):
    save_json(STATE_FILE, state)


def can_send(state, key, cooldown_minutes):
    """
    Prevent repeated email spam.
    """
    now = datetime.now(timezone.utc)
    last = state.get(key)

    if not last:
        state[key] = now.isoformat()
        return True

    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        state[key] = now.isoformat()
        return True

    if now - last_dt >= timedelta(minutes=cooldown_minutes):
        state[key] = now.isoformat()
        return True

    return False


# ============================================================
# Email
# ============================================================

def send_email(to_email, subject, body):
    print("=" * 90)
    print(f"EMAIL TO: {to_email}")
    print(f"SUBJECT: {subject}")
    print(body)
    print("=" * 90)

    if DRY_RUN:
        print("DRY_RUN=true, not sending real email.")
        return

    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    if EMAIL_PORT == 465:
        with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, timeout=30) as smtp:
            smtp.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            smtp.send_message(msg)


def alert_julie(subject, body):
    send_email(JULIE_EMAIL, subject, body)


def alert_parent(subject, body):
    send_email(PARENT_EMAIL, subject, body)


def alert_both(subject, julie_body, parent_body=None):
    alert_julie(subject, julie_body)
    alert_parent(subject, parent_body or julie_body)


# ============================================================
# Nightscout API
# ============================================================

def ns_get(path, params=None):
    params = params or {}
    if NS_TOKEN:
        params["token"] = NS_TOKEN

    url = f"{NS_URL}{path}"
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def safe_ns_get(path, params=None, default=None):
    try:
        return ns_get(path, params)
    except Exception as e:
        print(f"WARNING: failed to fetch {path}: {repr(e)}")
        return default


def parse_ns_time(item):
    if not isinstance(item, dict):
        return None

    if "date" in item and isinstance(item["date"], (int, float)):
        return datetime.fromtimestamp(item["date"] / 1000, tz=timezone.utc)

    for key in ["dateString", "created_at", "timestamp", "sysTime"]:
        value = item.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                pass

    return None


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fmt_local(dt):
    if not dt:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p")


def age_hours_from_dt(dt):
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def fmt_hours_as_age(hours):
    if hours is None:
        return "unknown"

    if hours < 0:
        return f"{hours:.1f}h future"

    days = int(hours // 24)
    hrs = int(round(hours % 24))

    if days > 0:
        return f"{days}d{hrs}h"
    return f"{hrs}h"


# ============================================================
# Local age txt files
# ============================================================

def read_time_file(path):
    """
    Read one ISO timestamp from podage.txt or dexcomage.txt.
    Only first non-empty line is used.
    Comments starting with # are ignored.
    """
    if not path.exists():
        return None, f"{path.name} does not exist"

    raw = path.read_text().strip()
    if not raw:
        return None, f"{path.name} is empty"

    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return None, f"{path.name} has no timestamp line"

    timestamp = lines[0]
    dt = parse_iso_datetime(timestamp)

    if dt is None:
        return None, f"{path.name} parse error; raw={timestamp}"

    return dt, None


def write_time_file_atomic(path, dt, reason):
    """
    Atomic write to avoid partial file if disk/write problem happens.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_utc = dt.astimezone(timezone.utc)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    content = (
        f"{dt_utc.isoformat().replace('+00:00', 'Z')}\n"
        f"# updated_at={datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}\n"
        f"# reason={reason}\n"
    )

    tmp_path.write_text(content)
    tmp_path.replace(path)


# ============================================================
# Treatment event detection
# ============================================================

def is_pod_change_event(treatment):
    event_type = str(treatment.get("eventType", "")).lower()
    notes = str(treatment.get("notes", "")).lower()

    if "site change" in event_type and "pod" in notes:
        return True

    if "pod change" in notes:
        return True

    if "pod" in event_type and "change" in event_type:
        return True

    return False


def is_dexcom_change_event(treatment):
    """
    Julie's current Nightscout output did not show Sensor Change events,
    but this is kept in case Loop/Nightscout starts uploading them later.
    """
    event_type = str(treatment.get("eventType", "")).lower()
    notes = str(treatment.get("notes", "")).lower()
    entered_by = str(treatment.get("enteredBy", "")).lower()

    text = f"{event_type} {notes} {entered_by}"

    if not any(w in text for w in ["sensor", "dexcom", "g7"]):
        return False

    if any(w in text for w in ["stop", "stopped", "remove", "removed", "expired", "end", "ended"]):
        return False

    return any(w in text for w in ["change", "changed", "start", "started", "insert", "inserted", "new"])


def latest_event_time(treatments, predicate):
    events = []

    for tr in treatments:
        try:
            if not predicate(tr):
                continue

            t = parse_ns_time(tr)
            if t:
                events.append((t.astimezone(timezone.utc), tr))
        except Exception:
            continue

    if not events:
        return None, None

    events.sort(key=lambda x: x[0], reverse=True)
    return events[0]


def sync_age_file_from_nightscout(state, file_path, latest_ns_time, device_name, cooldown_key):
    """
    Keeps local podage.txt / dexcomage.txt synchronized.

    Rules:
    - If local file is missing/bad and Nightscout has event: recover file from Nightscout.
    - If Nightscout has newer event: update local file.
    - If local file is valid and newer/same: keep local.
    - If local file bad and Nightscout has no event: alert parent.
    """
    local_time, local_error = read_time_file(file_path)

    # Case 1: local file invalid or missing.
    if local_time is None:
        if latest_ns_time is not None:
            write_time_file_atomic(
                file_path,
                latest_ns_time,
                f"auto recovered {device_name} from Nightscout event",
            )

            if can_send(state, f"{cooldown_key}_recovered_file", 720):
                alert_parent(
                    f"Julie {device_name} age file recovered",
                    (
                        f"{file_path.name} was missing or invalid:\n"
                        f"{local_error}\n\n"
                        f"I recovered it from Nightscout.\n"
                        f"Recovered time: {fmt_local(latest_ns_time)}\n"
                    ),
                )

            return latest_ns_time, f"{file_path.name} recovered from Nightscout"

        if can_send(state, f"{cooldown_key}_missing_file", 720):
            alert_parent(
                f"Julie {device_name} age file problem",
                (
                    f"{file_path.name} is missing or invalid:\n"
                    f"{local_error}\n\n"
                    f"I could not find a matching {device_name} change event in Nightscout.\n"
                    f"Please update {file_path.name} manually.\n\n"
                    f"Example:\n"
                    f"echo '2026-07-01T07:30:00-04:00' > {file_path.name}"
                ),
            )

        return None, f"{file_path.name} invalid and no Nightscout event"

    # Case 2: Nightscout has newer event.
    if latest_ns_time is not None and latest_ns_time > local_time + timedelta(minutes=5):
        write_time_file_atomic(
            file_path,
            latest_ns_time,
            f"auto updated from Nightscout {device_name} change event",
        )

        if can_send(state, f"{cooldown_key}_auto_updated", 360):
            alert_parent(
                f"Julie {device_name} age file auto-updated",
                (
                    f"I found a newer {device_name} change event in Nightscout.\n\n"
                    f"Old local time: {fmt_local(local_time)}\n"
                    f"New Nightscout time: {fmt_local(latest_ns_time)}\n"
                    f"Updated file: {file_path.name}"
                ),
            )

        return latest_ns_time, f"{file_path.name} auto-updated from Nightscout"

    # Case 3: local is valid and not older than Nightscout.
    return local_time, f"{file_path.name}"


def check_age_file_staleness(
    state,
    device_name,
    file_path,
    start_time,
    age_hours,
    stale_hours,
    cooldown_key,
    latest_ns_time=None,
):
    if start_time is None or age_hours is None:
        return

    if age_hours < -1:
        if can_send(state, f"{cooldown_key}_future_time", 720):
            alert_parent(
                f"Julie {device_name} age file has future time",
                (
                    f"{file_path.name} has a future timestamp.\n\n"
                    f"Recorded time: {fmt_local(start_time)}\n"
                    f"Calculated age: {age_hours:.1f} hours\n\n"
                    f"Please check and correct {file_path.name}."
                ),
            )
        return

    if age_hours >= stale_hours:
        latest_text = fmt_local(latest_ns_time) if latest_ns_time else "not found in recent Nightscout data"

        if can_send(state, f"{cooldown_key}_stale_record", 720):
            alert_parent(
                f"Julie {device_name} age record may be stale",
                (
                    f"{file_path.name} looks outdated.\n\n"
                    f"Recorded time: {fmt_local(start_time)}\n"
                    f"Calculated age: {age_hours:.1f} hours / {fmt_hours_as_age(age_hours)}\n"
                    f"Stale threshold: {stale_hours:.1f} hours\n"
                    f"Latest matching Nightscout event found: {latest_text}\n\n"
                    f"This may mean the {device_name} was changed but the local file was not updated, "
                    f"or there was a disk/write problem.\n\n"
                    f"Please check Julie's Nightscout/Loop and correct {file_path.name} if needed."
                ),
            )


# ============================================================
# Generic nested JSON helpers
# ============================================================

def walk_json(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            yield p, k, v
            yield from walk_json(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            yield from walk_json(v, p)


def parse_float(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(float(value)):
            return float(value)

    if isinstance(value, str):
        m = re.search(r"-?\d+(\.\d+)?", value)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None

    return None


# ============================================================
# Loop info
# ============================================================

def get_loop_info(devicestatus):
    if not devicestatus:
        return {}

    latest_dev = devicestatus[0]
    loop = latest_dev.get("loop", {}) or {}

    predicted = loop.get("predicted", {}) or {}
    predicted_values = predicted.get("values", []) or []

    cob_obj = loop.get("cob", {}) or {}
    iob_obj = loop.get("iob", {}) or {}

    clean_predicted_values = []
    for x in predicted_values:
        if isinstance(x, (int, float)):
            clean_predicted_values.append(float(x))

    return {
        "recommended_bolus": loop.get("recommendedBolus"),
        "cob": cob_obj.get("cob"),
        "iob": iob_obj.get("iob"),
        "predicted_values": clean_predicted_values,
        "loop_timestamp": loop.get("timestamp"),
    }


# ============================================================
# Exact reservoir if Nightscout ever exposes it
# ============================================================

def extract_reservoir_units(devicestatus, status):
    """
    Try to find exact pump reservoir / remaining insulin.

    Your current Nightscout data did NOT expose this field.
    This remains here in case Loop/Nightscout starts uploading it later.

    Do not use status.extendedSettings.pump.warnRes or urgentRes here;
    those are alert thresholds, not actual remaining insulin.
    """
    reservoir_keys = {
        "reservoir",
        "pumpReservoir",
        "pump_reservoir",
        "remainingInsulin",
        "remaining_insulin",
        "insulinRemaining",
        "insulin_remaining",
        "reservoirRemaining",
        "reservoir_remaining",
        "remainingReservoir",
        "remaining_reservoir",
    }

    candidates = []

    for source_name, source in [("devicestatus", devicestatus), ("status", status)]:
        for path, key, value in walk_json(source):
            key_lower = str(key).lower()
            path_lower = path.lower()

            # Avoid status extendedSettings thresholds.
            if "extendedsettings" in path_lower:
                continue

            if key in reservoir_keys:
                number = parse_float(value)
                if number is not None and 0 <= number <= 300:
                    candidates.append((source_name, path, number))

            if "reservoir" in path_lower and "iob" not in path_lower:
                number = parse_float(value)
                if number is not None and 0 <= number <= 300:
                    candidates.append((source_name, path, number))

            if "remaining" in path_lower and "insulin" in path_lower:
                number = parse_float(value)
                if number is not None and 0 <= number <= 300:
                    candidates.append((source_name, path, number))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: (
        0 if x[0] == "devicestatus" else 1,
        0 if "pump" in x[1].lower() else 1,
        len(x[1]),
    ))

    source_name, path, units = candidates[0]
    return units, f"{source_name}:{path}"


# ============================================================
# Delivered insulin estimate
# ============================================================

def delivered_insulin_since(treatments, start_time):
    """
    Estimate insulin delivered since pod start.

    Includes:
    - Bolus/correction treatment insulin
    - Loop Temp Basal treatment amount

    This is an estimate, not medical dosing guidance.
    """
    if not start_time:
        return None

    start_utc = start_time.astimezone(timezone.utc)
    total = 0.0

    for tr in treatments:
        t = parse_ns_time(tr)
        if not t:
            continue

        t = t.astimezone(timezone.utc)
        if t < start_utc:
            continue

        event_type = str(tr.get("eventType", "")).lower()

        insulin = tr.get("insulin")
        if isinstance(insulin, (int, float)) and insulin > 0:
            total += float(insulin)
            continue

        amount = tr.get("amount")
        if isinstance(amount, (int, float)) and amount > 0:
            if "temp basal" in event_type or "basal" in event_type:
                total += float(amount)

    return total


def estimate_remaining_insulin(treatments, pod_start_time):
    delivered = delivered_insulin_since(treatments, pod_start_time)
    if delivered is None:
        return None, None

    remaining = POD_INITIAL_UNITS - delivered
    return max(0.0, remaining), delivered


# ============================================================
# Missing bolus detection
# ============================================================

def is_carb_event(treatment):
    carbs = treatment.get("carbs")
    return isinstance(carbs, (int, float)) and carbs >= 5


def is_bolus_or_correction_event(treatment):
    """
    Count real bolus/correction insulin.
    Ignore automatic Temp Basal.
    """
    event_type = str(treatment.get("eventType", "")).lower()
    entered_by = str(treatment.get("enteredBy", "")).lower()
    insulin = treatment.get("insulin")

    if "temp basal" in event_type:
        return False

    if "bolus" in event_type or "correction" in event_type or "meal" in event_type:
        return True

    if isinstance(insulin, (int, float)) and insulin > 0:
        return True

    if "bolus" in entered_by:
        return True

    return False


def treatment_in_window(treatment, start_time, end_time):
    t = parse_ns_time(treatment)
    if not t:
        return False

    t = t.astimezone(timezone.utc)
    return start_time <= t <= end_time


def has_carb_or_bolus_in_last_hour(treatments, reference_time):
    end_time = reference_time.astimezone(timezone.utc)
    start_time = end_time - timedelta(minutes=MISSING_BOLUS_LOOKBACK_MIN)

    for tr in treatments:
        if not treatment_in_window(tr, start_time, end_time):
            continue

        if is_carb_event(tr):
            return True

        if is_bolus_or_correction_event(tr):
            return True

    return False


def linear_regression_slope(points):
    """
    points: list of (datetime, glucose), sorted ascending.
    Returns slope in mg/dL per minute.
    """
    if len(points) < 2:
        return 0.0

    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 60 for t, _ in points]
    ys = [g for _, g in points]

    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)

    if denominator == 0:
        return 0.0

    return numerator / denominator


def rise_consistency_score(points):
    """
    Measures whether glucose is mostly not dropping.
    Allows small CGM noise.
    """
    if len(points) < 2:
        return 0.0

    ok = 0
    total = 0

    for (_, g0), (_, g1) in zip(points, points[1:]):
        delta = g1 - g0
        total += 1

        # Allow small CGM noise drop.
        if delta >= -5:
            ok += 1

    return ok / total if total else 0.0


def detect_missing_bolus(entries, treatments, reference_time):
    """
    User-defined missing bolus logic:

    - BG steadily increased over previous 1 hour.
    - No carbs registered in previous 1 hour.
    - No bolus/correction insulin registered in previous 1 hour.
    - Ignore Loop automatic Temp Basal.
    """
    end_time = reference_time.astimezone(timezone.utc)
    start_time = end_time - timedelta(minutes=MISSING_BOLUS_LOOKBACK_MIN)

    points = []

    for e in entries:
        t = parse_ns_time(e)
        sgv = e.get("sgv")

        if not t or not isinstance(sgv, (int, float)):
            continue

        t = t.astimezone(timezone.utc)
        if start_time <= t <= end_time:
            points.append((t, float(sgv)))

    points.sort(key=lambda x: x[0])

    if len(points) < MISSING_BOLUS_MIN_POINTS:
        return None

    duration_min = (points[-1][0] - points[0][0]).total_seconds() / 60
    if duration_min < MISSING_BOLUS_MIN_DURATION:
        return None

    start_bg = points[0][1]
    current_bg = points[-1][1]
    net_rise = current_bg - start_bg
    slope = linear_regression_slope(points)
    consistency = rise_consistency_score(points)

    if current_bg < MISSING_BOLUS_MIN_CURRENT_BG:
        return None

    if net_rise < MISSING_BOLUS_MIN_RISE:
        return None

    if slope < MISSING_BOLUS_MIN_SLOPE:
        return None

    if consistency < 0.75:
        return None

    if has_carb_or_bolus_in_last_hour(treatments, reference_time):
        return None

    return {
        "start_bg": start_bg,
        "current_bg": current_bg,
        "net_rise": net_rise,
        "duration_min": duration_min,
        "slope": slope,
        "consistency": consistency,
        "points": len(points),
    }


# ============================================================
# Device age alerts
# ============================================================

def check_pod_age_alert(state, pod_start_time, pod_age_hours, pod_source):
    if pod_age_hours is None:
        return

    if pod_age_hours >= POD_EXPIRE_HOURS:
        if can_send(state, "pod_age_expired", 180):
            alert_both(
                f"Julie pod may be expired: {fmt_hours_as_age(pod_age_hours)}",
                (
                    f"Julie, your pod appears to be about {fmt_hours_as_age(pod_age_hours)} old.\n\n"
                    f"Please check Loop/Pod and replace if needed."
                ),
                (
                    f"Julie pod age alert.\n"
                    f"Pod start: {fmt_local(pod_start_time)}\n"
                    f"Pod age: {pod_age_hours:.1f} hours / {fmt_hours_as_age(pod_age_hours)}\n"
                    f"Source: {pod_source}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    elif pod_age_hours >= POD_WARN_HOURS:
        if can_send(state, "pod_age_warn", 360):
            alert_both(
                f"Julie pod nearing 72h: {fmt_hours_as_age(pod_age_hours)}",
                (
                    f"Julie, your pod is getting close to 72 hours.\n\n"
                    f"Pod age: {fmt_hours_as_age(pod_age_hours)}\n"
                    f"Consider whether to replace before sleep or school."
                ),
                (
                    f"Julie pod nearing 72h.\n"
                    f"Pod start: {fmt_local(pod_start_time)}\n"
                    f"Pod age: {pod_age_hours:.1f} hours / {fmt_hours_as_age(pod_age_hours)}\n"
                    f"Source: {pod_source}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )


def check_dexcom_age_alert(state, dexcom_start_time, dexcom_age_hours, dexcom_source):
    if dexcom_age_hours is None:
        return

    if dexcom_age_hours >= DEXCOM_URGENT_HOURS:
        if can_send(state, "dexcom_age_urgent", 180):
            alert_both(
                f"Julie Dexcom near end: {fmt_hours_as_age(dexcom_age_hours)}",
                (
                    f"Julie, your Dexcom sensor appears to be about {fmt_hours_as_age(dexcom_age_hours)} old.\n\n"
                    f"It may be near the end of the grace period. Please check Dexcom and replace if needed."
                ),
                (
                    f"Julie Dexcom urgent age alert.\n"
                    f"Dexcom start: {fmt_local(dexcom_start_time)}\n"
                    f"Dexcom age: {dexcom_age_hours:.1f} hours / {fmt_hours_as_age(dexcom_age_hours)}\n"
                    f"Source: {dexcom_source}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    elif dexcom_age_hours >= DEXCOM_REPLACE_HOURS:
        if can_send(state, "dexcom_age_replace", 360):
            alert_both(
                f"Julie Dexcom replacement reminder: {fmt_hours_as_age(dexcom_age_hours)}",
                (
                    f"Julie, your Dexcom sensor appears to be about {fmt_hours_as_age(dexcom_age_hours)} old.\n\n"
                    f"Please consider replacing it at a convenient time, especially before sleep or school."
                ),
                (
                    f"Julie Dexcom replacement reminder.\n"
                    f"Dexcom start: {fmt_local(dexcom_start_time)}\n"
                    f"Dexcom age: {dexcom_age_hours:.1f} hours / {fmt_hours_as_age(dexcom_age_hours)}\n"
                    f"Source: {dexcom_source}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    elif dexcom_age_hours >= DEXCOM_WARN_HOURS:
        if can_send(state, "dexcom_age_warn", 720):
            alert_both(
                f"Julie Dexcom nearing 10 days: {fmt_hours_as_age(dexcom_age_hours)}",
                (
                    f"Julie, your Dexcom sensor is getting close to 10 days.\n\n"
                    f"Dexcom age: {fmt_hours_as_age(dexcom_age_hours)}\n"
                    f"Consider whether to replace before sleep or school."
                ),
                (
                    f"Julie Dexcom nearing 10 days.\n"
                    f"Dexcom start: {fmt_local(dexcom_start_time)}\n"
                    f"Dexcom age: {dexcom_age_hours:.1f} hours / {fmt_hours_as_age(dexcom_age_hours)}\n"
                    f"Source: {dexcom_source}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )


def check_pod_insulin_alert(
    state,
    insulin_left,
    insulin_source,
    pod_start_time,
    pod_age_hours,
):
    if insulin_left is None:
        return

    if insulin_left <= POD_INSULIN_URGENT_U:
        if can_send(state, "pod_insulin_urgent", 60):
            alert_both(
                f"Julie pod insulin very low: {insulin_left:.1f}U",
                (
                    f"Julie, pod insulin may be very low.\n\n"
                    f"Estimated/known remaining insulin: {insulin_left:.1f}U\n"
                    f"Please check Loop/Pod and prepare to replace the pod."
                ),
                (
                    f"Julie pod insulin urgent.\n"
                    f"Remaining insulin: {insulin_left:.1f}U\n"
                    f"Source: {insulin_source}\n"
                    f"Pod start: {fmt_local(pod_start_time)}\n"
                    f"Pod age: {fmt_hours_as_age(pod_age_hours)}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    elif insulin_left < POD_INSULIN_WARN_U:
        if can_send(state, "pod_insulin_low", 120):
            alert_both(
                f"Julie pod insulin low: {insulin_left:.1f}U",
                (
                    f"Julie, pod insulin may be getting low.\n\n"
                    f"Estimated/known remaining insulin: {insulin_left:.1f}U\n"
                    f"Consider replacing before sleep or school."
                ),
                (
                    f"Julie pod insulin low reminder.\n"
                    f"Remaining insulin: {insulin_left:.1f}U\n"
                    f"Source: {insulin_source}\n"
                    f"Pod start: {fmt_local(pod_start_time)}\n"
                    f"Pod age: {fmt_hours_as_age(pod_age_hours)}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )


# ============================================================
# Main
# ============================================================

def main():
    state = load_alert_state()
    now = datetime.now(timezone.utc)

    # More entries needed for missing bolus 1-hour trend.
    entries = ns_get("/api/v1/entries/sgv.json", {"count": 30})

    # More treatments needed for pod-age and delivered-insulin estimate.
    treatments = ns_get("/api/v1/treatments.json", {"count": 3000})

    devicestatus = ns_get("/api/v1/devicestatus.json", {"count": 20})
    status = safe_ns_get("/api/v1/status.json", default={}) or {}

    # ------------------------------------------------------------
    # Sync local podage.txt / dexcomage.txt
    # ------------------------------------------------------------

    latest_pod_time, latest_pod_event = latest_event_time(treatments, is_pod_change_event)
    latest_dexcom_time, latest_dexcom_event = latest_event_time(treatments, is_dexcom_change_event)

    pod_start_time, pod_source = sync_age_file_from_nightscout(
        state=state,
        file_path=PODAGE_FILE,
        latest_ns_time=latest_pod_time,
        device_name="pod",
        cooldown_key="podage",
    )

    dexcom_start_time, dexcom_source = sync_age_file_from_nightscout(
        state=state,
        file_path=DEXCOMAGE_FILE,
        latest_ns_time=latest_dexcom_time,
        device_name="Dexcom",
        cooldown_key="dexcomage",
    )

    pod_age_hours = age_hours_from_dt(pod_start_time)
    dexcom_age_hours = age_hours_from_dt(dexcom_start_time)

    check_age_file_staleness(
        state=state,
        device_name="pod",
        file_path=PODAGE_FILE,
        start_time=pod_start_time,
        age_hours=pod_age_hours,
        stale_hours=POD_RECORD_STALE_HOURS,
        cooldown_key="podage",
        latest_ns_time=latest_pod_time,
    )

    check_age_file_staleness(
        state=state,
        device_name="Dexcom",
        file_path=DEXCOMAGE_FILE,
        start_time=dexcom_start_time,
        age_hours=dexcom_age_hours,
        stale_hours=DEXCOM_RECORD_STALE_HOURS,
        cooldown_key="dexcomage",
        latest_ns_time=latest_dexcom_time,
    )

    # ------------------------------------------------------------
    # Basic CGM validation
    # ------------------------------------------------------------

    if not entries:
        if can_send(state, "no_entries", 20):
            alert_parent(
                "Julie Nightscout no CGM entries",
                f"Julie Nightscout returned no CGM entries.\n\nNightscout: {NS_URL}",
            )
        save_alert_state(state)
        return

    latest = entries[0]
    latest_time = parse_ns_time(latest)
    bg = latest.get("sgv")
    direction = latest.get("direction", "")

    if not latest_time:
        if can_send(state, "bad_cgm_time", 30):
            alert_parent(
                "Julie Nightscout CGM time parse error",
                f"Could not parse latest CGM time.\n\nLatest entry:\n{json.dumps(latest, indent=2)}",
            )
        save_alert_state(state)
        return

    age_min = int((now - latest_time.astimezone(timezone.utc)).total_seconds() / 60)

    if age_min > STALE_MINUTES:
        if can_send(state, "stale_data", 20):
            alert_both(
                "Julie Nightscout data stale",
                (
                    f"Julie, Nightscout/Dexcom data may be stale.\n\n"
                    f"Last CGM update: {fmt_local(latest_time)}\n"
                    f"Age: {age_min} minutes\n\n"
                    f"Please check Dexcom/Loop connection."
                ),
                (
                    f"Julie Nightscout data may be stale.\n"
                    f"Last CGM update: {fmt_local(latest_time)}\n"
                    f"Age: {age_min} minutes\n"
                    f"Nightscout: {NS_URL}"
                ),
            )
        save_alert_state(state)
        return

    # ------------------------------------------------------------
    # Loop prediction / IOB / COB
    # ------------------------------------------------------------

    loop_info = get_loop_info(devicestatus)

    predicted_values = loop_info.get("predicted_values", [])
    recommended_bolus = loop_info.get("recommended_bolus")
    cob = loop_info.get("cob")
    iob = loop_info.get("iob")

    # Loop predicted values are usually 5-minute intervals.
    pred_30 = predicted_values[:7]
    pred_60 = predicted_values[:13]
    pred_90 = predicted_values[:19]

    min_pred_30 = min(pred_30) if pred_30 else None
    min_pred_60 = min(pred_60) if pred_60 else None
    max_pred_60 = max(pred_60) if pred_60 else None
    max_pred_90 = max(pred_90) if pred_90 else None

    # ------------------------------------------------------------
    # Pump insulin: exact reservoir if available, else estimate
    # ------------------------------------------------------------

    reservoir_units, reservoir_source = extract_reservoir_units(devicestatus, status)

    estimated_remaining = None
    estimated_delivered = None

    if reservoir_units is None:
        estimated_remaining, estimated_delivered = estimate_remaining_insulin(treatments, pod_start_time)

    if reservoir_units is not None:
        insulin_left = reservoir_units
        insulin_source = f"Nightscout exact reservoir: {reservoir_source}"
    else:
        insulin_left = estimated_remaining
        if estimated_remaining is not None:
            insulin_source = (
                f"estimated from {POD_INITIAL_UNITS:.0f}U initial fill "
                f"minus delivered insulin {estimated_delivered:.1f}U since pod start"
            )
        else:
            insulin_source = "unknown"

    # ------------------------------------------------------------
    # Device age alerts
    # ------------------------------------------------------------

    check_pod_age_alert(state, pod_start_time, pod_age_hours, pod_source)
    check_dexcom_age_alert(state, dexcom_start_time, dexcom_age_hours, dexcom_source)
    check_pod_insulin_alert(state, insulin_left, insulin_source, pod_start_time, pod_age_hours)

    # ------------------------------------------------------------
    # 1. Low now
    # ------------------------------------------------------------

    if isinstance(bg, (int, float)) and bg <= LOW_NOW:
        if can_send(state, "low_now", 15):
            alert_both(
                f"URGENT: Julie BG {bg}",
                (
                    f"Julie, your BG is {bg} {direction}.\n\n"
                    f"Please check Dexcom/Loop now and follow your low treatment plan.\n\n"
                    f"Nightscout: {NS_URL}"
                ),
                (
                    f"Julie low alert sent.\n"
                    f"BG: {bg} {direction}\n"
                    f"Time: {fmt_local(latest_time)}\n"
                    f"IOB: {iob}\n"
                    f"COB: {cob}\n"
                    f"Predicted 30-min low: {min_pred_30}\n"
                    f"Predicted 60-min low: {min_pred_60}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # ------------------------------------------------------------
    # 2. Low soon from Loop prediction
    # ------------------------------------------------------------

    elif (
        (min_pred_30 is not None and min_pred_30 <= LOW_SOON_30_MIN)
        or
        (min_pred_60 is not None and min_pred_60 <= LOW_SOON_60_MIN)
    ):
        if can_send(state, "low_soon", 20):
            alert_both(
                f"Julie possible low soon: BG {bg}",
                (
                    f"Julie, Loop predicts your BG may go low soon.\n\n"
                    f"Current BG: {bg} {direction}\n"
                    f"Predicted 30-min low: {min_pred_30}\n"
                    f"Predicted 60-min low: {min_pred_60}\n\n"
                    f"Please check Dexcom/Loop."
                ),
                (
                    f"Julie possible low-soon alert sent.\n"
                    f"Current BG: {bg} {direction}\n"
                    f"Predicted 30-min low: {min_pred_30}\n"
                    f"Predicted 60-min low: {min_pred_60}\n"
                    f"IOB: {iob}\n"
                    f"COB: {cob}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # ------------------------------------------------------------
    # 3. High now
    # ------------------------------------------------------------

    if isinstance(bg, (int, float)) and bg >= HIGH_NOW:
        if can_send(state, "high_now", 45):
            alert_both(
                f"Julie high BG {bg}",
                (
                    f"Julie, your BG is {bg} {direction}.\n\n"
                    f"Please check Loop/IOB, pod site, and follow your usual high-BG plan.\n\n"
                    f"Nightscout: {NS_URL}"
                ),
                (
                    f"Julie high alert sent.\n"
                    f"BG: {bg} {direction}\n"
                    f"Time: {fmt_local(latest_time)}\n"
                    f"IOB: {iob}\n"
                    f"COB: {cob}\n"
                    f"Recommended bolus: {recommended_bolus}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # ------------------------------------------------------------
    # 4. High predicted
    # ------------------------------------------------------------

    if (
        max_pred_90 is not None
        and max_pred_90 >= HIGH_PREDICTED
        and isinstance(bg, (int, float))
        and bg >= 140
    ):
        if can_send(state, "high_predicted", 60):
            alert_both(
                f"Julie BG may go high: {bg}",
                (
                    f"Julie, Loop predicts your BG may go high.\n\n"
                    f"Current BG: {bg} {direction}\n"
                    f"Predicted 60-min high: {max_pred_60}\n"
                    f"Predicted 90-min high: {max_pred_90}\n\n"
                    f"Please check Loop and follow your usual plan."
                ),
                (
                    f"Julie high-predicted alert sent.\n"
                    f"Current BG: {bg} {direction}\n"
                    f"Predicted 60-min high: {max_pred_60}\n"
                    f"Predicted 90-min high: {max_pred_90}\n"
                    f"IOB: {iob}\n"
                    f"COB: {cob}\n"
                    f"Recommended bolus: {recommended_bolus}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # ------------------------------------------------------------
    # 5. Missing bolus: steady rise + no carbs + no bolus
    # ------------------------------------------------------------

    missing = detect_missing_bolus(entries, treatments, latest_time)

    if missing:
        if can_send(state, "missing_bolus_steady_rise", 60):
            alert_both(
                "Julie possible missed bolus",
                (
                    f"Julie, your BG has been steadily rising for about 1 hour, "
                    f"and Nightscout does not show carbs or a recent bolus/correction.\n\n"
                    f"Start BG: {missing['start_bg']:.0f}\n"
                    f"Current BG: {missing['current_bg']:.0f} {direction}\n"
                    f"Rise: +{missing['net_rise']:.0f} mg/dL over {missing['duration_min']:.0f} min\n\n"
                    f"Please check Loop and make sure food/bolus was entered if needed."
                ),
                (
                    f"Julie possible missed bolus detected.\n"
                    f"Reason: steady BG rise over previous hour with no carb or bolus/correction treatment.\n\n"
                    f"Start BG: {missing['start_bg']:.0f}\n"
                    f"Current BG: {missing['current_bg']:.0f} {direction}\n"
                    f"Rise: +{missing['net_rise']:.0f} mg/dL\n"
                    f"Duration: {missing['duration_min']:.0f} min\n"
                    f"Slope: {missing['slope']:.2f} mg/dL/min\n"
                    f"Consistency: {missing['consistency']:.2f}\n"
                    f"IOB: {iob}\n"
                    f"COB: {cob}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # ------------------------------------------------------------
    # Save state and print debug
    # ------------------------------------------------------------

    save_alert_state(state)

    print("Agent run complete.")
    print(f"BG: {bg} {direction}")
    print(f"CGM time: {fmt_local(latest_time)}")
    print(f"CGM age: {age_min} min")
    print(f"IOB: {iob}")
    print(f"COB: {cob}")
    print(f"Recommended bolus: {recommended_bolus}")
    print(f"Predicted 30-min low: {min_pred_30}")
    print(f"Predicted 60-min low: {min_pred_60}")
    print(f"Predicted 90-min high: {max_pred_90}")

    print(f"Pod start: {fmt_local(pod_start_time) if pod_start_time else None}")
    print(f"Pod source: {pod_source}")
    print(f"Pod age: {pod_age_hours:.1f}h / {fmt_hours_as_age(pod_age_hours) if pod_age_hours is not None else None}")

    print(f"Dexcom start: {fmt_local(dexcom_start_time) if dexcom_start_time else None}")
    print(f"Dexcom source: {dexcom_source}")
    print(f"Dexcom age: {dexcom_age_hours:.1f}h / {fmt_hours_as_age(dexcom_age_hours) if dexcom_age_hours is not None else None}")

    print(f"Exact reservoir from API: {reservoir_units}")
    print(f"Exact reservoir source: {reservoir_source}")
    print(f"Estimated delivered insulin since pod start: {estimated_delivered}")
    print(f"Estimated remaining insulin: {estimated_remaining}")
    print(f"Insulin left used by alert logic: {insulin_left}")
    print(f"Insulin source: {insulin_source}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", repr(e))
        raise
