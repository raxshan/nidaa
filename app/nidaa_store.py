"""File-backed storage for the dashboard (plan sections 2.3-2.5).

Every config write goes through save_config(): validate with the scheduler's own
validator, check the expected version, back up, then write atomically. The
dashboard never plays audio; it queues commands that the scheduler executes.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import nidaa_scheduler as sched

BACKUP_KEEP = 20
STATUS_FRESH_SECONDS = 90
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
UPLOAD_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg")
EVENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_write_lock = threading.Lock()


class ConfigError(Exception):
    """Validation failed; .problems holds human-readable messages."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class VersionConflict(Exception):
    """config.json changed since the caller read it."""


class NotFound(Exception):
    pass


def audit_path():
    return sched.CONFIG_PATH.parent / "audit.jsonl"


def now_local(settings=None):
    tz = sched.ZoneInfo((settings or {}).get("timezone", sched.DEFAULT_SETTINGS["timezone"]))
    return datetime.now(tz)


# ============================================================
# CONFIG READ / WRITE
# ============================================================

def read_config():
    """Returns (raw_dict, version). Raw keeps unknown keys and key order."""
    data = sched.CONFIG_PATH.read_bytes()
    return json.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()[:16]


def current_version():
    return read_config()[1]


def parsed(raw):
    """Scheduler Config object for a raw dict (raises ValueError on bad settings)."""
    config, _ = sched.config_from_dict(raw)
    return config


def validate_raw(raw):
    """All problems with a full config dict (empty list means OK to save)."""
    problems = []
    try:
        config, event_problems = sched.config_from_dict(raw)
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]
    problems.extend(event_problems)
    events = raw.get("events", [])
    if not isinstance(events, list):
        return ["'events' must be a list"]
    known_devices = set(config.devices)
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id", "?")
        if isinstance(eid, str) and not EVENT_ID_RE.match(eid):
            problems.append(f"{eid}: id may only use a-z, 0-9, '-' and '_' (max 64)")
        devices = (ev.get("action") or {}).get("devices") or {}
        if isinstance(devices, dict):
            for name in devices:
                if name not in known_devices:
                    problems.append(f"{eid}: unknown device '{name}' (add it under Settings first)")
    for name, spec in config.devices.items():
        if not isinstance(spec, dict):
            problems.append(f"device '{name}' must be an object")
    for key in ("grace_seconds", "conflict_window_minutes", "history_days"):
        value = config.settings.get(key)
        if not sched._is_number(value) or value < 0:
            problems.append(f"settings.{key} must be a non-negative number")
    dv = config.settings.get("default_volume")
    if not sched._is_number(dv) or not 0 <= dv <= 1:
        problems.append("settings.default_volume must be between 0 and 1")
    return problems


def _dump(raw):
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _write_atomic(path, text):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_backups():
    pattern = sched.CONFIG_PATH.name + ".bak-"
    out = []
    for path in sched.CONFIG_PATH.parent.glob(pattern + "*"):
        if path.suffix == ".tmp":
            continue
        stat = path.stat()
        out.append({"name": path.name, "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")})
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def _backup_current():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = sched.CONFIG_PATH.parent / f"{sched.CONFIG_PATH.name}.bak-{stamp}"
    target, n = base, 1
    while target.exists():
        n += 1
        target = base.with_name(f"{base.name}-{n}")
    shutil.copy2(sched.CONFIG_PATH, target)
    for old in list_backups()[BACKUP_KEEP:]:
        try:
            (sched.CONFIG_PATH.parent / old["name"]).unlink()
        except OSError:
            pass
    return target.name


def save_config(new_raw, expected_version, actor="dashboard", action="save", detail=None):
    """Validate, check version, back up, write atomically. Returns the new version."""
    problems = validate_raw(new_raw)
    if problems:
        raise ConfigError(problems)
    with _write_lock:
        _, version = read_config()
        if expected_version is not None and expected_version != version:
            raise VersionConflict(
                "config.json was changed by someone else since you opened it. "
                "Reload and make your change again.")
        backup = _backup_current()
        _write_atomic(sched.CONFIG_PATH, _dump(new_raw))
        new_version = current_version()
    audit(actor, action, detail, backup=backup)
    return new_version


def restore_backup(name, expected_version, actor="dashboard"):
    if name not in {b["name"] for b in list_backups()}:
        raise NotFound(f"no backup named {name}")
    raw = json.loads((sched.CONFIG_PATH.parent / name).read_text(encoding="utf-8"))
    return save_config(raw, expected_version, actor, "restore", {"backup": name})


def audit(actor, action, detail=None, **extra):
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "actor": actor, "action": action}
    if detail is not None:
        entry["detail"] = detail
    entry.update(extra)
    try:
        with open(audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ============================================================
# EVENTS
# ============================================================

def find_event(raw, event_id):
    for index, ev in enumerate(raw.get("events", [])):
        if isinstance(ev, dict) and ev.get("id") == event_id:
            return index, ev
    raise NotFound(f"no event with id '{event_id}'")


def is_prayer_event(ev):
    return (ev.get("trigger") or {}).get("type") == "prayer" or str(ev.get("id", "")).startswith("prayer-")


def create_event(event, expected_version, actor="dashboard"):
    raw, _ = read_config()
    if not isinstance(event, dict) or not event.get("id"):
        raise ConfigError(["event needs an 'id'"])
    if any(isinstance(e, dict) and e.get("id") == event["id"] for e in raw.get("events", [])):
        raise ConfigError([f"an event with id '{event['id']}' already exists"])
    raw.setdefault("events", []).append(event)
    return save_config(raw, expected_version, actor, "create_event", {"id": event["id"]})


def update_event(event_id, event, expected_version, actor="dashboard", confirm_prayer=False):
    raw, _ = read_config()
    index, old = find_event(raw, event_id)
    if (is_prayer_event(old) or is_prayer_event(event)) and not confirm_prayer:
        raise ConfigError(["changing a prayer event needs confirmation (tick the confirm box)"])
    if event.get("id") != event_id and any(
            isinstance(e, dict) and e.get("id") == event.get("id") for e in raw["events"]):
        raise ConfigError([f"an event with id '{event.get('id')}' already exists"])
    raw["events"][index] = event
    return save_config(raw, expected_version, actor, "update_event", {"id": event_id})


def delete_event(event_id, expected_version, actor="dashboard", confirm_prayer=False):
    raw, _ = read_config()
    index, old = find_event(raw, event_id)
    if is_prayer_event(old) and not confirm_prayer:
        raise ConfigError(["deleting a prayer event needs confirmation"])
    del raw["events"][index]
    return save_config(raw, expected_version, actor, "delete_event", {"id": event_id})


def unique_copy_id(raw, base):
    ids = {e.get("id") for e in raw.get("events", []) if isinstance(e, dict)}
    candidate, n = f"{base}-copy", 2
    while candidate in ids:
        candidate, n = f"{base}-copy{n}", n + 1
    return candidate[:64]


WEEKDAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
                  "fri": "Fri", "sat": "Sat", "sun": "Sun"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _days_label(days):
    if not days or set(days) == set(sched.WEEKDAYS):
        return "every day"
    if set(days) == {"mon", "tue", "wed", "thu", "fri"}:
        return "weekdays"
    if set(days) == {"sat", "sun"}:
        return "weekends"
    return ", ".join(WEEKDAY_LABELS[d] for d in sched.WEEKDAYS if d in days)


def describe_trigger(trigger):
    """Human-readable schedule text for an event trigger."""
    t = trigger.get("type")
    try:
        if t == "prayer":
            off = int(trigger.get("offset_minutes", 0))
            base = trigger["prayer"]
            if off:
                base = f"{abs(off)} min {'before' if off < 0 else 'after'} {base}"
            days = trigger.get("days")
            return base + (f" ({_days_label(days)})" if days else "")
        if t == "weekly":
            return f"{_days_label(trigger['days']).capitalize()} at {trigger['time']}"
        if t == "yearly":
            return f"Every {MONTHS[int(trigger['month']) - 1]} {int(trigger['day'])} at {trigger['time']}"
        if t == "once":
            dt = datetime.fromisoformat(trigger["datetime"])
            return f"Once: {MONTHS[dt.month - 1]} {dt.day}, {dt.year} at {dt.strftime('%H:%M')}"
    except (KeyError, ValueError, TypeError, IndexError):
        pass
    return str(t)


def next_occurrence(event, config, now, horizon_days=400):
    """Next fire time of one event (ignores conflicts), or None."""
    if not event.get("enabled", True):
        return None
    single = copy.copy(config)
    single.events = [event]
    if event["trigger"]["type"] == "once":
        dt = datetime.fromisoformat(event["trigger"]["datetime"])
        dt = dt.replace(tzinfo=config.tz) if dt.tzinfo is None else dt.astimezone(config.tz)
        days = [dt.date()]
    else:
        days = (now.date() + timedelta(days=i) for i in range(horizon_days))
    for day in days:
        for occ in sched.occurrences_for_day(single, day):
            if sched.seconds_between(now, occ.when) > 0:
                return occ.when
    return None


def schedule_for_day(config, day, state=None):
    """Concrete occurrences on a day, with conflict + fired status."""
    window = sched.occurrences_for_day(config, day - timedelta(days=1)) + \
        sched.occurrences_for_day(config, day)
    skips = sched.pending_skips()
    sched.apply_conflicts(window, config.settings["conflict_window_minutes"], skips)
    fired = (state or {}).get("fired", {})
    now = datetime.now(config.tz)
    out = []
    for occ in window:
        if occ.when.date() != day:
            continue
        status = "past" if sched.seconds_between(occ.when, now) > config.settings["grace_seconds"] else "scheduled"
        if occ.key in fired:
            status = fired[occ.key]["status"]
        elif occ.suppressed_by:
            status = "suppressed"
        elif occ.event["id"] in skips:
            status = "skip pending"
        out.append({"event": occ.event["id"], "name": occ.event.get("name") or occ.event["id"],
                    "type": occ.event["trigger"]["type"], "when": occ.when.isoformat(),
                    "time": occ.when.strftime("%I:%M %p").lstrip("0"),
                    "status": status, "suppressed_by": occ.suppressed_by})
    return out


# ============================================================
# STATUS / HISTORY / STATE
# ============================================================

def read_status():
    status = sched.read_json(sched.STATUS_PATH, None)
    if not isinstance(status, dict):
        return {"alive": False, "age_seconds": None, "devices": {}, "next": [], "last_error": None}
    age = None
    try:
        updated = datetime.fromisoformat(status["updated"])
        age = (datetime.now(updated.tzinfo) - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        pass
    status["age_seconds"] = age
    status["alive"] = age is not None and age <= STATUS_FRESH_SECONDS
    return status


def read_state():
    return sched.read_json(sched.STATE_PATH, {"fired": {}})


def read_history(limit=200, device=None, status=None, event=None, since=None):
    path = sched.HISTORY_PATH
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if device and row.get("device") != device:
            continue
        if status and row.get("status") != status:
            continue
        if event and row.get("event") != event:
            continue
        if since and str(row.get("ts", "")) < since:
            continue
        rows.append(row)
    rows.reverse()
    return rows[:limit]


# ============================================================
# COMMANDS (dashboard -> scheduler)
# ============================================================

def queue_command(action, actor="dashboard", **fields):
    if action not in sched.COMMAND_ACTIONS:
        raise ConfigError([f"unknown action {action}"])
    cmd_id = uuid.uuid4().hex[:12]
    cmd = {"id": cmd_id, "action": action, "created": now_local().isoformat(timespec="seconds"),
           "actor": actor, **fields}
    sched.COMMANDS_DIR.mkdir(exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{cmd_id}.json"
    tmp = sched.COMMANDS_DIR / (name + ".tmp")   # *.tmp is ignored by the scheduler
    tmp.write_text(json.dumps(cmd), encoding="utf-8")
    os.replace(tmp, sched.COMMANDS_DIR / name)
    audit(actor, f"command:{action}", {k: v for k, v in cmd.items() if k not in ("created",)})
    return cmd


def command_result(cmd_id):
    if not re.fullmatch(r"[0-9a-f]{12}", cmd_id or ""):
        raise NotFound("bad command id")
    for folder, pending in ((sched.COMMANDS_DIR / "done", False), (sched.COMMANDS_DIR, True)):
        for path in folder.glob(f"*-{cmd_id}.json"):
            data = sched.read_json(path, {})
            data["pending"] = pending
            return data
    raise NotFound("command not found")


def queue_play_now(audio, devices, max_seconds, config, actor="dashboard"):
    """Validate a manual play request server-side (never trust the UI)."""
    problems = []
    if not sched.safe_audio_name(audio):
        problems.append("choose an audio file")
    elif not (sched.AUDIO_DIR / audio).is_file():
        problems.append(f"audio file not found: {audio}")
    if not isinstance(devices, dict) or not devices:
        problems.append("choose at least one device")
        devices = {}
    cap = config.settings["max_volume"]
    clean = {}
    for name, vol in devices.items():
        if name not in config.devices:
            problems.append(f"unknown device '{name}'")
            continue
        if not sched._is_number(vol) or not 0 <= vol <= 1:
            problems.append(f"{name}: volume must be between 0 and 1")
            continue
        clean[name] = round(min(float(vol), cap), 2)
    if max_seconds is not None and (not sched._is_number(max_seconds) or not 0 < max_seconds <= 3600):
        problems.append("max seconds must be between 1 and 3600")
    if problems:
        raise ConfigError(problems)
    fields = {"audio": audio, "devices": clean}
    if max_seconds:
        fields["max_seconds"] = max_seconds
    return queue_command("play_now", actor, **fields)


def queue_stop(devices, config, actor="dashboard"):
    devices = devices or list(config.devices)
    unknown = [d for d in devices if d not in config.devices]
    if unknown:
        raise ConfigError([f"unknown device(s): {', '.join(unknown)}"])
    return queue_command("stop", actor, devices=devices)


# ============================================================
# AUDIO LIBRARY
# ============================================================

def _duration(path):
    try:
        import mutagen  # optional
    except ImportError:
        return None
    try:
        info = mutagen.File(path)
        return round(info.info.length) if info and info.info else None
    except Exception:
        return None


def list_audio(config):
    refs_enabled = sched.referenced_audio(config, enabled_only=True)
    refs_all = sched.referenced_audio(config, enabled_only=False)
    out = []
    if not sched.AUDIO_DIR.is_dir():
        return out
    for path in sorted(sched.AUDIO_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or not sched.safe_audio_name(path.name):
            continue
        out.append({"name": path.name, "size": path.stat().st_size, "duration": _duration(path),
                    "used_by": refs_all.get(path.name, []),
                    "used_by_enabled": refs_enabled.get(path.name, [])})
    return out


def audio_names():
    if not sched.AUDIO_DIR.is_dir():
        return []
    return sorted((p.name for p in sched.AUDIO_DIR.iterdir()
                   if p.is_file() and sched.safe_audio_name(p.name)), key=str.lower)


def sanitize_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"\s+", "-", stem)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "", stem).strip(".-_")
    ext = ext.lower()
    if not stem or ext not in UPLOAD_EXTENSIONS:
        raise ConfigError([f"file must be one of {', '.join(UPLOAD_EXTENSIONS)} with a usable name"])
    return (stem[:100] + ext)


def _looks_like(ext, head):
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext == ".m4a":
        return head[4:8] == b"ftyp"
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".ogg":
        return head[:4] == b"OggS"
    return False


def save_upload(filename, fileobj, overwrite=False, actor="dashboard"):
    """Stream an upload into the library with type, size and name checks."""
    name = sanitize_filename(filename)
    target = sched.AUDIO_DIR / name
    if target.resolve().parent != sched.AUDIO_DIR.resolve():
        raise ConfigError(["bad file name"])
    if target.exists() and not overwrite:
        raise ConfigError([f"{name} already exists; tick 'overwrite' to replace it"])
    sched.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = sched.AUDIO_DIR / f".upload-{uuid.uuid4().hex}.tmp"
    size, head = 0, b""
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                if len(head) < 16:
                    head += chunk[:16 - len(head)]
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    raise ConfigError([f"file is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB"])
                out.write(chunk)
        if size == 0:
            raise ConfigError(["file is empty"])
        if not _looks_like(os.path.splitext(name)[1], head):
            raise ConfigError(["file content does not look like the audio type its name says"])
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    audit(actor, "upload_audio", {"name": name, "size": size, "overwrite": overwrite})
    return name


def delete_audio(name, config, actor="dashboard"):
    if not sched.safe_audio_name(name):
        raise ConfigError(["bad file name"])
    path = sched.AUDIO_DIR / name
    if path.resolve().parent != sched.AUDIO_DIR.resolve() or not path.is_file():
        raise NotFound(f"no audio file named {name}")
    users = sched.referenced_audio(config, enabled_only=True).get(name, [])
    if users:
        raise ConfigError([f"{name} is used by enabled event(s): {', '.join(users)}"])
    path.unlink()
    audit(actor, "delete_audio", {"name": name})


# ============================================================
# MISC
# ============================================================

def calculation_methods():
    return sorted(m.name for m in sched.CalculationMethod)


def today_prayer_times(config, day=None):
    day = day or datetime.now(config.tz).date()
    try:
        return {k: v for k, v in sched.get_prayer_times(config, day).items()}
    except Exception:
        return {}


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ConfigError(["date must be YYYY-MM-DD"])
