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

# Optional Verizon email-to-text
ENABLE_TEXT = os.environ.get("ENABLE_TEXT", "false").strip().lower() in {"1", "true", "yes", "y"}
JULIE_TEXT_EMAIL = os.environ.get("JULIE_TEXT_EMAIL", "").strip()
PARENT_TEXT_EMAIL = os.environ.get("PARENT_TEXT_EMAIL", "").strip()
TEXT_MAX_CHARS = int(os.environ.get("TEXT_MAX_CHARS", "150"))

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

# Dexcom / CGM freshness
STALE_MINUTES = float(os.environ.get("STALE_MINUTES", "15"))

# Uploader phone battery / Loop health
UPLOADER_BATTERY_WARN = float(os.environ.get("UPLOADER_BATTERY_WARN", "30"))
UPLOADER_BATTERY_URGENT = float(os.environ.get("UPLOADER_BATTERY_URGENT", "20"))

DEVICESTATUS_STALE_MINUTES = float(os.environ.get("DEVICESTATUS_STALE_MINUTES", "15"))
LOOP_STALE_MINUTES = float(os.environ.get("LOOP_STALE_MINUTES", "15"))
PUMP_CLOCK_STALE_MINUTES = float(os.environ.get("PUMP_CLOCK_STALE_MINUTES", "30"))

# Pod age / Omnipod age
POD_WARN_HOURS = float(os.environ.get("POD_WARN_HOURS", "68"))
POD_EXPIRE_HOURS = float(os.environ.get("POD_EXPIRE_HOURS", "72"))
POD_RECORD_STALE_HOURS = float(os.environ.get("POD_RECORD_STALE_HOURS", "84"))

# Dexcom G7 age
DEXCOM_WARN_HOURS = float(os.environ.get("DEXCOM_WARN_HOURS", str(9 * 24 + 18)))       # 9d18h
DEXCOM_REPLACE_HOURS = float(os.environ.get("DEXCOM_REPLACE_HOURS", str(10 * 24)))     # 10d
DEXCOM_URGENT_HOURS = float(os.environ.get("DEXCOM_URGENT_HOURS", str(10 * 24 + 10)))  # 10d10h
DEXCOM_RECORD_STALE_HOURS = float(os.environ.get("DEXCOM_RECORD_STALE_HOURS", "264"))  # 11d

# Pump insulin estimation
POD_INITIAL_UNITS = float(os.environ.get("POD_INITIAL_UNITS", "200"))
POD_INSULIN_WARN_U = float(os.environ.get("POD_INSULIN_WARN_U", "10"))
POD_INSULIN_URGENT_U = float(os.environ.get("POD_INSULIN_URGENT_U", "5"))

# Missing carb / missing bolus logic
MISSING_BOLUS_LOOKBACK_MIN = float(os.environ.get("MISSING_BOLUS_LOOKBACK_MIN", "60"))
MISSING_BOLUS_MIN_CURRENT_BG = float(os.environ.get("MISSING_BOLUS_MIN_CURRENT_BG", "150"))
MISSING_BOLUS_MIN_RISE = float(os.environ.get("MISSING_BOLUS_MIN_RISE", "30"))
MISSING_BOLUS_MIN_SLOPE = float(os.environ.get("MISSING_BOLUS_MIN_SLOPE", "0.30"))
MISSING_BOLUS_MIN_DURATION = float(os.environ.get("MISSING_BOLUS_MIN_DURATION", "30"))
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
    Prevent repeated email/text spam.
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
# Email + Verizon email-to-text
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


def make_text_message(subject, body):
    """
    Create a short SMS-style message for Verizon email-to-text.
    Keep it short because vtext.com can cut or reject long messages.
    """
    first_line = ""

    for line in body.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break

    msg = subject.strip()

    if first_line and first_line not in msg:
        msg = f"{msg}: {first_line}"

    msg = " ".join(msg.split())

    if len(msg) > TEXT_MAX_CHARS:
        msg = msg[:TEXT_MAX_CHARS - 3] + "..."

    return msg


def send_text_email(to_text_email, subject, body):
    """
    Send a text message using Verizon email-to-text.
    Example:
      2155551234@vtext.com
      2155551234@vzwpix.com
    """
    if not ENABLE_TEXT:
        return

    if not to_text_email:
        return

    text = make_text_message(subject, body)

    print("=" * 90)
    print(f"TEXT EMAIL TO: {to_text_email}")
    print(text)
    print("=" * 90)

    if DRY_RUN:
        print("DRY_RUN=true, not sending real text email.")
        return

    msg = EmailMessage()
    msg["From"] = EMAIL_USER
    msg["To"] = to_text_email
    msg["Subject"] = ""
    msg.set_content(text)

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
    send_text_email(JULIE_TEXT_EMAIL, subject, body)


def alert_parent(subject, body):
    send_email(PARENT_EMAIL, subject, body)
    send_text_email(PARENT_TEXT_EMAIL, subject, body)


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


def parse_ns_time_value(value):
    if not value:
        return None

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

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


def minutes_old(dt):
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60


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
    Only first non-empty non-comment line is used.
    """
    if not path.exists():
        return None, f"{path.name} does not exist"

    raw = path.read_text().strip()
    if not raw:
        return None, f"{path.name} is empty"

    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

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
# Loop / devicestatus info
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


def get_latest_devicestatus_info(devicestatus):
    """
    Extract freshness/battery/loop/pump status from latest devicestatus.
    """
    if not devicestatus:
        return {
            "latest_dev": None,
            "dev_created_at": None,
            "dev_age_min": None,
            "uploader_name": None,
            "uploader_battery": None,
            "uploader_timestamp": None,
            "uploader_age_min": None,
            "loop_timestamp": None,
            "loop_age_min": None,
            "pump_clock": None,
            "pump_clock_age_min": None,
            "pump_suspended": None,
            "pump_bolusing": None,
            "pump_id": None,
            "pump_model": None,
        }

    latest_dev = devicestatus[0]

    dev_created_at = parse_ns_time(latest_dev)
    dev_age_min = minutes_old(dev_created_at)

    uploader = latest_dev.get("uploader", {}) or {}
    uploader_timestamp = parse_ns_time_value(uploader.get("timestamp"))
    uploader_age_min = minutes_old(uploader_timestamp)
    uploader_battery = uploader.get("battery")

    loop = latest_dev.get("loop", {}) or {}
    loop_timestamp = parse_ns_time_value(loop.get("timestamp"))
    loop_age_min = minutes_old(loop_timestamp)

    pump = latest_dev.get("pump", {}) or {}
    pump_clock = parse_ns_time_value(pump.get("clock"))
    pump_clock_age_min = minutes_old(pump_clock)

    return {
        "latest_dev": latest_dev,
        "dev_created_at": dev_created_at,
        "dev_age_min": dev_age_min,
        "uploader_name": uploader.get("name"),
        "uploader_battery": uploader_battery,
        "uploader_timestamp": uploader_timestamp,
        "uploader_age_min": uploader_age_min,
        "loop_timestamp": loop_timestamp,
        "loop_age_min": loop_age_min,
        "pump_clock": pump_clock,
        "pump_clock_age_min": pump_clock_age_min,
        "pump_suspended": pump.get("suspended"),
        "pump_bolusing": pump.get("bolusing"),
        "pump_id": pump.get("pumpID"),
        "pump_model": pump.get("model"),
    }


def check_loop_and_device_health(state, dev_info, latest_cgm_time):
    """
    Monitor:
    - Nightscout devicestatus stale
    - Loop stopped / stale
    - uploader phone battery low
    - pump clock stale
    - pump suspended
    """

    dev_age = dev_info.get("dev_age_min")
    loop_age = dev_info.get("loop_age_min")
    uploader_age = dev_info.get("uploader_age_min")
    pump_clock_age = dev_info.get("pump_clock_age_min")
    uploader_battery = dev_info.get("uploader_battery")
    pump_suspended = dev_info.get("pump_suspended")

    # 1. No devicestatus / devicestatus stale.
    if dev_age is None:
        if can_send(state, "devicestatus_missing", 30):
            alert_parent(
                "Julie Nightscout devicestatus missing",
                (
                    f"Nightscout returned no usable devicestatus.\n\n"
                    f"CGM last update: {fmt_local(latest_cgm_time)}\n"
                    f"Nightscout: {NS_URL}\n\n"
                    f"Please check Loop/Nightscout uploader."
                ),
            )
    elif dev_age > DEVICESTATUS_STALE_MINUTES:
        if can_send(state, "devicestatus_stale", 30):
            alert_both(
                "Julie Loop/Nightscout status may be stale",
                (
                    f"Julie, Loop/Nightscout status may not be updating.\n\n"
                    f"Last Loop status upload: {fmt_local(dev_info.get('dev_created_at'))}\n"
                    f"Age: {dev_age:.0f} minutes\n\n"
                    f"Please check Loop app, phone internet, and Nightscout upload."
                ),
                (
                    f"Julie devicestatus stale.\n"
                    f"Last devicestatus: {fmt_local(dev_info.get('dev_created_at'))}\n"
                    f"Age: {dev_age:.1f} minutes\n"
                    f"Uploader: {dev_info.get('uploader_name')}\n"
                    f"Uploader battery: {uploader_battery}\n"
                    f"Loop timestamp: {fmt_local(dev_info.get('loop_timestamp'))}\n"
                    f"Pump clock: {fmt_local(dev_info.get('pump_clock'))}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # 2. Loop stale.
    if loop_age is None:
        if can_send(state, "loop_timestamp_missing", 30):
            alert_parent(
                "Julie Loop timestamp missing",
                (
                    f"Latest devicestatus does not include loop.timestamp.\n\n"
                    f"This may mean Loop data is not uploading correctly.\n"
                    f"Nightscout: {NS_URL}"
                ),
            )
    elif loop_age > LOOP_STALE_MINUTES:
        if can_send(state, "loop_stale", 20):
            alert_both(
                "Julie Loop may have stopped updating",
                (
                    f"Julie, Loop may not be updating.\n\n"
                    f"Last Loop timestamp: {fmt_local(dev_info.get('loop_timestamp'))}\n"
                    f"Age: {loop_age:.0f} minutes\n\n"
                    f"Please open Loop and check that it is running."
                ),
                (
                    f"Julie Loop stale alert.\n"
                    f"Last Loop timestamp: {fmt_local(dev_info.get('loop_timestamp'))}\n"
                    f"Loop age: {loop_age:.1f} minutes\n"
                    f"Devicestatus age: {dev_age}\n"
                    f"Uploader battery: {uploader_battery}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # 3. Uploader battery low.
    if isinstance(uploader_battery, (int, float)):
        if uploader_battery <= UPLOADER_BATTERY_URGENT:
            if can_send(state, "uploader_battery_urgent", 60):
                alert_both(
                    f"Julie phone battery very low: {uploader_battery:.0f}%",
                    (
                        f"Julie, your Loop uploader phone battery appears very low: {uploader_battery:.0f}%.\n\n"
                        f"Please charge it so Dexcom/Loop/Nightscout can keep working."
                    ),
                    (
                        f"Julie uploader battery urgent.\n"
                        f"Battery: {uploader_battery:.0f}%\n"
                        f"Uploader timestamp: {fmt_local(dev_info.get('uploader_timestamp'))}\n"
                        f"Uploader age: {uploader_age}\n"
                        f"Nightscout: {NS_URL}"
                    ),
                )

        elif uploader_battery <= UPLOADER_BATTERY_WARN:
            if can_send(state, "uploader_battery_warn", 180):
                alert_both(
                    f"Julie phone battery low: {uploader_battery:.0f}%",
                    (
                        f"Julie, your Loop uploader phone battery is low: {uploader_battery:.0f}%.\n\n"
                        f"Please charge it when convenient."
                    ),
                    (
                        f"Julie uploader battery low.\n"
                        f"Battery: {uploader_battery:.0f}%\n"
                        f"Uploader timestamp: {fmt_local(dev_info.get('uploader_timestamp'))}\n"
                        f"Nightscout: {NS_URL}"
                    ),
                )

    # 4. Pump clock stale / pump communication may be stale.
    if pump_clock_age is not None and pump_clock_age > PUMP_CLOCK_STALE_MINUTES:
        if can_send(state, "pump_clock_stale", 30):
            alert_both(
                "Julie pump communication may be stale",
                (
                    f"Julie, pump communication may be stale.\n\n"
                    f"Last pump clock: {fmt_local(dev_info.get('pump_clock'))}\n"
                    f"Age: {pump_clock_age:.0f} minutes\n\n"
                    f"Please check Loop/Pod communication."
                ),
                (
                    f"Julie pump clock stale.\n"
                    f"Pump clock: {fmt_local(dev_info.get('pump_clock'))}\n"
                    f"Pump clock age: {pump_clock_age:.1f} minutes\n"
                    f"Pump model: {dev_info.get('pump_model')}\n"
                    f"Pump ID: {dev_info.get('pump_id')}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )

    # 5. Pump suspended.
    if pump_suspended is True:
        if can_send(state, "pump_suspended", 15):
            alert_both(
                "URGENT: Julie pump appears suspended",
                (
                    f"Julie, Nightscout shows the pump may be suspended.\n\n"
                    f"Please check Loop/Pod immediately."
                ),
                (
                    f"Julie pump suspended alert.\n"
                    f"Pump suspended: {pump_suspended}\n"
                    f"Pump model: {dev_info.get('pump_model')}\n"
                    f"Pump ID: {dev_info.get('pump_id')}\n"
                    f"Pump clock: {fmt_local(dev_info.get('pump_clock'))}\n"
                    f"Nightscout: {NS_URL}"
                ),
            )


# ============================================================
# Exact reservoir if Nightscout ever exposes it
# ============================================================

def extract_reservoir_units(devicestatus, status):
    """
    Try to find exact pump reservoir / remaining insulin.

    Current Julie Nightscout data may not expose this field.
    Do not use status.extendedSettings.pump.warnRes or urgentRes;
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
            path_lower = path.lower()

            # Avoid Nightscout settings thresholds.
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
# Missing carb / bolus detection
# ============================================================

def treatment_in_window(treatment, start_time, end_time):
    t = parse_ns_time(treatment)
    if not t:
        return False

    t = t.astimezone(timezone.utc)
    return start_time <= t <= end_time


def get_recent_carb_and_bolus_summary(treatments, reference_time, lookback_min=60):
    """
    Summarize recent carb / meaningful bolus events.

    Important:
    - Ignore automatic Temp Basal.
    - Ignore tiny automatic Loop insulin events unless they are meaningful.
    """
    end_time = reference_time.astimezone(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_min)

    carb_events = []
    meaningful_bolus_events = []
    automatic_insulin_events = []

    for tr in treatments:
        if not treatment_in_window(tr, start_time, end_time):
            continue

        event_type = str(tr.get("eventType", "")).lower()
        entered_by = str(tr.get("enteredBy", "")).lower()
        automatic = bool(tr.get("automatic", False))

        carbs = tr.get("carbs")
        insulin = tr.get("insulin")
        amount = tr.get("amount")

        if isinstance(carbs, (int, float)) and carbs >= 5:
            carb_events.append(tr)

        # Ignore temp basal for missed meal/carb detection.
        if "temp basal" in event_type:
            if isinstance(amount, (int, float)) and amount > 0:
                automatic_insulin_events.append(tr)
            continue

        is_meaningful = False

        if "meal bolus" in event_type:
            is_meaningful = True
        elif "carb correction" in event_type:
            is_meaningful = True
        elif "correction bolus" in event_type:
            # Tiny automatic corrections should not hide missed meal/carb alerts.
            if not automatic:
                is_meaningful = True
            elif isinstance(insulin, (int, float)) and insulin >= 0.5:
                is_meaningful = True
        elif "bolus" in event_type and not automatic:
            is_meaningful = True
        elif isinstance(insulin, (int, float)) and insulin >= 0.5 and not automatic:
            is_meaningful = True

        if is_meaningful:
            meaningful_bolus_events.append(tr)
        else:
            if isinstance(insulin, (int, float)) and insulin > 0:
                automatic_insulin_events.append(tr)
            elif isinstance(amount, (int, float)) and amount > 0:
                automatic_insulin_events.append(tr)

    return {
        "carb_events": carb_events,
        "meaningful_bolus_events": meaningful_bolus_events,
        "automatic_insulin_events": automatic_insulin_events,
    }


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


def detect_missing_bolus(entries, treatments, reference_time, cob=None):
    """
    Possible missed carb / missed bolus detection:

    - BG rising steadily over previous 45–60 min.
    - Current BG high enough.
    - No carb entry.
    - No meaningful meal/correction/manual bolus.
    - COB is 0 or low.
    - Ignore Loop Temp Basal and tiny automatic events.
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
        print(f"Missing bolus debug: no alert; not enough CGM points: {len(points)}")
        return None

    duration_min = (points[-1][0] - points[0][0]).total_seconds() / 60
    if duration_min < MISSING_BOLUS_MIN_DURATION:
        print(f"Missing bolus debug: no alert; duration too short: {duration_min:.1f} min")
        return None

    start_bg = points[0][1]
    current_bg = points[-1][1]
    net_rise = current_bg - start_bg
    slope = linear_regression_slope(points)
    consistency = rise_consistency_score(points)

    summary = get_recent_carb_and_bolus_summary(
        treatments,
        reference_time,
        lookback_min=MISSING_BOLUS_LOOKBACK_MIN,
    )

    carb_count = len(summary["carb_events"])
    meaningful_bolus_count = len(summary["meaningful_bolus_events"])
    automatic_insulin_count = len(summary["automatic_insulin_events"])

    print(
        "Missing bolus debug: "
        f"start_bg={start_bg:.0f}, current_bg={current_bg:.0f}, "
        f"net_rise={net_rise:.0f}, duration={duration_min:.0f}min, "
        f"slope={slope:.2f}, consistency={consistency:.2f}, "
        f"COB={cob}, carbs={carb_count}, meaningful_bolus={meaningful_bolus_count}, "
        f"automatic_insulin_events={automatic_insulin_count}"
    )

    if current_bg < MISSING_BOLUS_MIN_CURRENT_BG:
        print("Missing bolus debug: no alert; current BG below threshold")
        return None

    if net_rise < MISSING_BOLUS_MIN_RISE:
        print("Missing bolus debug: no alert; net rise below threshold")
        return None

    if slope < MISSING_BOLUS_MIN_SLOPE:
        print("Missing bolus debug: no alert; slope below threshold")
        return None

    if consistency < 0.70:
        print("Missing bolus debug: no alert; rise not consistent enough")
        return None

    if isinstance(cob, (int, float)) and cob > 5:
        print("Missing bolus debug: no alert; COB exists")
        return None

    if carb_count > 0:
        print("Missing bolus debug: no alert; carb event found")
        return None

    if meaningful_bolus_count > 0:
        print("Missing bolus debug: no alert; meaningful bolus/correction found")
        return None

    return {
        "start_bg": start_bg,
        "current_bg": current_bg,
        "net_rise": net_rise,
        "duration_min": duration_min,
        "slope": slope,
        "consistency": consistency,
        "points": len(points),
        "carb_count": carb_count,
        "meaningful_bolus_count": meaningful_bolus_count,
        "automatic_insulin_count": automatic_insulin_count,
    }


# ============================================================
# Device age / insulin alerts
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
                "Julie Dexcom/CGM data stale",
                (
                    f"Julie, Dexcom/CGM data may be stale.\n\n"
                    f"Last CGM update: {fmt_local(latest_time)}\n"
                    f"Age: {age_min} minutes\n\n"
                    f"Please check Dexcom, Bluetooth, Loop, and Nightscout upload."
                ),
                (
                    f"Julie Dexcom/CGM data may be stale.\n"
                    f"Last CGM update: {fmt_local(latest_time)}\n"
                    f"Age: {age_min} minutes\n"
                    f"Nightscout: {NS_URL}"
                ),
            )
        save_alert_state(state)
        return

    # ------------------------------------------------------------
    # Loop / phone battery / pump health monitor
    # ------------------------------------------------------------

    dev_info = get_latest_devicestatus_info(devicestatus)
    check_loop_and_device_health(state, dev_info, latest_time)

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
    # Device age and insulin alerts
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
        high_key = "high_now"
        high_cooldown = 45

       # If BG is high and still rising, remind more frequently.
        if bg >= HIGH_NOW and str(direction) in {"SingleUp", "DoubleUp", "FortyFiveUp"}:
            high_key = "high_now_rising"
            high_cooldown = 20

        # If very high, use shorter cooldown even if trend text is not recognized.
        if bg >= 300:
            high_key = "very_high_now"
            high_cooldown = 15

        if can_send(state, high_key, high_cooldown):
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
    # 5. Missing carb / bolus: steady rise + no carbs + no meaningful bolus
    # ------------------------------------------------------------

    missing = detect_missing_bolus(entries, treatments, latest_time, cob=cob)

    if missing:
        if can_send(state, "missing_bolus_steady_rise", 60):
            alert_both(
                "Julie possible missed carb/bolus",
                (
                    f"Julie, your BG has been rising and Nightscout does not show recent carbs "
                    f"or a meaningful bolus/correction.\n\n"
                    f"Start BG: {missing['start_bg']:.0f}\n"
                    f"Current BG: {missing['current_bg']:.0f} {direction}\n"
                    f"Rise: +{missing['net_rise']:.0f} mg/dL over {missing['duration_min']:.0f} min\n\n"
                    f"Please check Loop and make sure food/bolus was entered if needed."
                ),
                (
                    f"Julie possible missed carb/bolus detected.\n"
                    f"Reason: steady BG rise with no carb entry and no meaningful bolus/correction.\n\n"
                    f"Start BG: {missing['start_bg']:.0f}\n"
                    f"Current BG: {missing['current_bg']:.0f} {direction}\n"
                    f"Rise: +{missing['net_rise']:.0f} mg/dL\n"
                    f"Duration: {missing['duration_min']:.0f} min\n"
                    f"Slope: {missing['slope']:.2f} mg/dL/min\n"
                    f"Consistency: {missing['consistency']:.2f}\n"
                    f"Automatic insulin events ignored: {missing['automatic_insulin_count']}\n"
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

    print(f"Devicestatus age: {dev_info.get('dev_age_min')}")
    print(f"Uploader: {dev_info.get('uploader_name')}")
    print(f"Uploader battery: {dev_info.get('uploader_battery')}")
    print(f"Uploader age: {dev_info.get('uploader_age_min')}")
    print(f"Loop timestamp: {fmt_local(dev_info.get('loop_timestamp')) if dev_info.get('loop_timestamp') else None}")
    print(f"Loop age: {dev_info.get('loop_age_min')}")
    print(f"Pump clock: {fmt_local(dev_info.get('pump_clock')) if dev_info.get('pump_clock') else None}")
    print(f"Pump clock age: {dev_info.get('pump_clock_age_min')}")
    print(f"Pump suspended: {dev_info.get('pump_suspended')}")
    print(f"Pump bolusing: {dev_info.get('pump_bolusing')}")

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
