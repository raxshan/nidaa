#!/usr/bin/env python3
"""
Nidaa scheduler - plays the Adhan and other scheduled audio on Google Cast devices.

Event triggers:  prayer | weekly | yearly | once
Config:          config.json  (hot-reloaded when the file changes, no restart)
State:           state.json   (which occurrences already fired; survives restarts)
Skip requests:   skips.json   ({"skip_next": ["event-id", ...]}; written by CLI/dashboard)
History:         history.jsonl (one JSON line per device playback attempt)

Commands:        commands/*.json (play_now | play_event | stop; written by the dashboard,
                 executed here so only one process ever talks to the Cast devices)
Heartbeat:       status.json   (written every 30 s; read by the dashboard)

CLI helpers (run with the same venv python):
    python nidaa_scheduler.py --check            validate config.json
    python nidaa_scheduler.py --today            show today's schedule
    python nidaa_scheduler.py --skip EVENT_ID    skip the next occurrence
    python nidaa_scheduler.py --play EVENT_ID    play an event right now (test)
"""

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pychromecast
from adhanpy.calculation import CalculationMethod
from adhanpy.calculation.CalculationParameters import CalculationParameters
from adhanpy.calculation.HighLatitudeRule import HighLatitudeRule
from adhanpy.calculation.Madhab import Madhab
from adhanpy.PrayerTimes import PrayerTimes


# ============================================================
# LOGGING
# ============================================================

# Prevent PyChromecast / Zeroconf retries from flooding syslog.
logging.getLogger("pychromecast").setLevel(logging.CRITICAL)
logging.getLogger("zeroconf").setLevel(logging.CRITICAL)


def log(message=""):
    print(message, flush=True)


# ============================================================
# PATHS / CONSTANTS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("NIDAA_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.environ.get("NIDAA_STATE", BASE_DIR / "state.json"))
SKIPS_PATH = Path(os.environ.get("NIDAA_SKIPS", BASE_DIR / "skips.json"))
HISTORY_PATH = Path(os.environ.get("NIDAA_HISTORY", BASE_DIR / "history.jsonl"))
AUDIO_DIR = Path(os.environ.get("NIDAA_AUDIO_DIR", BASE_DIR.parent / "audio"))
STATUS_PATH = Path(os.environ.get("NIDAA_STATUS", BASE_DIR / "status.json"))
COMMANDS_DIR = Path(os.environ.get("NIDAA_COMMANDS", BASE_DIR / "commands"))

VERSION = "2.1.0"
HEARTBEAT_SECONDS = 30
DEVICE_PROBE_SECONDS = 300
COMMAND_MAX_AGE_SECONDS = 300     # older commands are discarded, never played late
COMMAND_DONE_KEEP_DAYS = 7
COMMAND_ACTIONS = ("play_now", "play_event", "stop")

LOOP_SECONDS = 5
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg")
PRAYER_NAMES = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Higher number wins when two events fall within conflict_window_minutes.
DEFAULT_PRIORITY = {"prayer": 100, "once": 60, "yearly": 50, "weekly": 40}

DEFAULT_SETTINGS = {
    "latitude": 40.7128,
    "longitude": -74.006,
    "timezone": "America/New_York",
    "calculation_method": "NORTH_AMERICA",
    "audio_base_url": "http://192.168.1.50:8080/audio",
    "default_volume": 0.35,
    "max_volume": 1.0,             # hard cap, applied to every playback
    "grace_seconds": 120,          # fire late (e.g. after restart) within this window
    "conflict_window_minutes": 3,  # lower-priority events this close to a higher one are skipped
    "history_days": 90,
    # Optional: "SHAFI" (adhanpy default) or "HANAFI" (later Asr).
    "madhab": None,
    # Optional: "MIDDLE_OF_THE_NIGHT" (adhanpy default), "SEVENTH_OF_THE_NIGHT", "TWILIGHT_ANGLE".
    "high_latitude_rule": None,
}

MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


# ============================================================
# JSON HELPERS
# ============================================================

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        log(f"Could not read {path}: {exc}")
        return default


def write_json_atomic(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ============================================================
# CONFIG LOADING + VALIDATION
# ============================================================

class Config:
    def __init__(self, settings, devices, events):
        self.settings = settings
        self.devices = devices
        self.events = events
        self.tz = ZoneInfo(settings["timezone"])
        self.method = getattr(CalculationMethod, settings["calculation_method"])
        self.madhab = getattr(Madhab, settings["madhab"]) if settings.get("madhab") else None
        self.high_latitude_rule = (getattr(HighLatitudeRule, settings["high_latitude_rule"])
                                   if settings.get("high_latitude_rule") else None)


def _parse_hhmm(value):
    return datetime.strptime(value, "%H:%M").time()


def _parse_days(trigger, required):
    days = trigger.get("days")
    if days is None:
        if required:
            raise ValueError("'days' is required (e.g. [\"mon\",\"tue\"])")
        return None
    if not isinstance(days, list) or not days:
        raise ValueError("'days' must be a non-empty list")
    bad = [d for d in days if d not in WEEKDAYS]
    if bad:
        raise ValueError(f"unknown day(s) {bad}; use {WEEKDAYS}")
    return days


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_event(ev, seen_ids):
    """Return a list of error strings (empty means valid)."""
    if not isinstance(ev, dict):
        return ["event must be an object"]

    errors = []
    eid = ev.get("id")
    if not isinstance(eid, str) or not eid.strip():
        errors.append("missing 'id'")
    elif eid in seen_ids:
        errors.append("duplicate 'id'")

    trigger = ev.get("trigger")
    if not isinstance(trigger, dict):
        errors.append("missing 'trigger'")
        trigger = {}

    ttype = trigger.get("type")
    try:
        if ttype == "prayer":
            if trigger.get("prayer") not in PRAYER_NAMES:
                raise ValueError(f"'prayer' must be one of {PRAYER_NAMES}")
            int(trigger.get("offset_minutes", 0))
            _parse_days(trigger, required=False)
        elif ttype == "weekly":
            _parse_days(trigger, required=True)
            _parse_hhmm(trigger["time"])
        elif ttype == "yearly":
            date(2024, int(trigger["month"]), int(trigger["day"]))  # 2024 = leap year
            _parse_hhmm(trigger["time"])
        elif ttype == "once":
            datetime.fromisoformat(trigger["datetime"])
        else:
            raise ValueError(f"unknown trigger type {ttype!r}")
    except (KeyError, ValueError, TypeError) as exc:
        errors.append(f"bad trigger: {exc}")

    for field in ("active_from", "active_until"):
        if ev.get(field) is not None:
            try:
                date.fromisoformat(ev[field])
            except (ValueError, TypeError):
                errors.append(f"'{field}' must be YYYY-MM-DD")

    if "priority" in ev and not isinstance(ev["priority"], int):
        errors.append("'priority' must be an integer")

    action = ev.get("action")
    if not isinstance(action, dict) or not isinstance(action.get("devices"), dict) \
            or not action["devices"]:
        errors.append("'action.devices' must be a non-empty object")
        return errors

    if "max_seconds" in action and (not _is_number(action["max_seconds"])
                                    or action["max_seconds"] <= 0):
        errors.append("'action.max_seconds' must be a positive number")

    default_audio = action.get("audio")
    for name, spec in action["devices"].items():
        if _is_number(spec):
            audio, volume = default_audio, spec
        elif isinstance(spec, dict):
            audio, volume = spec.get("audio", default_audio), spec.get("volume", 0)
        else:
            errors.append(f"device '{name}': use a volume number or an object")
            continue
        if not audio:
            errors.append(f"device '{name}': no audio (set action.audio or per-device audio)")
        if not _is_number(volume) or not 0 <= volume <= 1:
            errors.append(f"device '{name}': volume must be between 0 and 1")

    return errors


def load_config(path=None):
    """Returns (Config, problems). Raises on unreadable/invalid top-level JSON."""
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return config_from_dict(raw)


def config_from_dict(raw):
    """Build a Config from an already-parsed dict. Raises on invalid settings."""
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    settings = {**DEFAULT_SETTINGS, **raw.get("settings", {})}
    devices = raw.get("devices", {})

    events, problems, seen = [], [], set()
    for index, ev in enumerate(raw.get("events", [])):
        label = ev.get("id", f"#{index}") if isinstance(ev, dict) else f"#{index}"
        errors = validate_event(ev, seen)
        if errors:
            problems.extend(f"{label}: {e}" for e in errors)
            continue
        seen.add(ev["id"])
        events.append(ev)

    if not isinstance(devices, dict):
        raise ValueError("'devices' must be an object")
    try:
        config = Config(settings, devices, events)
    except (AttributeError, KeyError, ValueError) as exc:
        raise ValueError(f"bad settings: {exc}") from exc
    cap = settings["max_volume"]
    if not _is_number(cap) or not 0 <= cap <= 1:
        raise ValueError("settings.max_volume must be between 0 and 1")
    return config, problems


def referenced_audio(config, enabled_only=True):
    """{audio_file: [event ids]} for every file an event would play."""
    refs = {}
    for ev in config.events:
        if enabled_only and not ev.get("enabled", True):
            continue
        action = ev["action"]
        for spec in action["devices"].values():
            audio = action.get("audio") if _is_number(spec) else spec.get("audio", action.get("audio"))
            if audio:
                refs.setdefault(audio, [])
                if ev["id"] not in refs[audio]:
                    refs[audio].append(ev["id"])
    return refs


def resolve_targets(event, config):
    """Return [(device_name, audio_file, volume)] with volume capped."""
    action = event["action"]
    default_audio = action.get("audio")
    default_volume = config.settings["default_volume"]
    cap = config.settings["max_volume"]

    targets = []
    for name, spec in action["devices"].items():
        if _is_number(spec):
            audio, volume = default_audio, spec
        else:
            audio = spec.get("audio", default_audio)
            volume = spec.get("volume", default_volume)
        targets.append((name, audio, min(float(volume), cap)))
    return targets


# ============================================================
# STATE (what already fired) + SKIP REQUESTS
# ============================================================

class State:
    """Scheduler-owned. Persisted so restarts never replay or lose an Adhan."""

    def __init__(self, path=None):
        self.path = path or STATE_PATH
        data = read_json(self.path, {})
        self.fired = data.get("fired", {})

    def mark(self, key, status):
        self.fired[key] = {"at": datetime.now().isoformat(timespec="seconds"),
                           "status": status}
        self.save()

    def prune(self, keep_days=7):
        cutoff = datetime.now() - timedelta(days=keep_days)
        before = len(self.fired)
        self.fired = {
            k: v for k, v in self.fired.items()
            if datetime.fromisoformat(v["at"]) >= cutoff
        }
        if len(self.fired) != before:
            self.save()

    def save(self):
        try:
            write_json_atomic(self.path, {"fired": self.fired})
        except OSError as exc:
            log(f"Could not save state: {exc}")


@contextlib.contextmanager
def file_lock(path):
    """Advisory lock shared by the scheduler, the CLI and the dashboard."""
    with open(str(path) + ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pending_skips():
    return set(read_json(SKIPS_PATH, {}).get("skip_next", []))


def add_skip(event_id):
    with file_lock(SKIPS_PATH):
        skips = read_json(SKIPS_PATH, {}).get("skip_next", [])
        if event_id not in skips:
            skips.append(event_id)
        write_json_atomic(SKIPS_PATH, {"skip_next": skips})


def remove_skip(event_id):
    """Cancel a pending skip request. True if one was pending."""
    with file_lock(SKIPS_PATH):
        skips = read_json(SKIPS_PATH, {}).get("skip_next", [])
        if event_id not in skips:
            return False
        skips.remove(event_id)
        write_json_atomic(SKIPS_PATH, {"skip_next": skips})
        return True


def consume_skip(event_id):
    """True (and removes the request) if a skip was pending for this event."""
    return remove_skip(event_id)


# ============================================================
# PRAYER TIMES
# ============================================================

def get_prayer_times(config, day):
    noon = datetime.combine(day, dtime(12, 0), tzinfo=config.tz)
    coords = (config.settings["latitude"], config.settings["longitude"])
    if config.madhab is None and config.high_latitude_rule is None:
        times = PrayerTimes(coords, noon, config.method, time_zone=config.tz)
    else:
        params = CalculationParameters(method=config.method)
        if config.madhab is not None:
            params.madhab = config.madhab
        if config.high_latitude_rule is not None:
            params.high_latitude_rule = config.high_latitude_rule
        times = PrayerTimes(coords, noon, calculation_parameters=params, time_zone=config.tz)

    def local(value):
        if value.tzinfo is None:
            return value.replace(tzinfo=config.tz)
        return value.astimezone(config.tz)

    return {
        "Fajr": local(times.fajr),
        "Dhuhr": local(times.dhuhr),
        "Asr": local(times.asr),
        "Maghrib": local(times.maghrib),
        "Isha": local(times.isha),
    }


# ============================================================
# OCCURRENCES  (event definitions -> concrete fire times for a day)
# ============================================================

class Occurrence:
    def __init__(self, event, when):
        self.event = event
        self.when = when
        self.priority = event.get(
            "priority", DEFAULT_PRIORITY[event["trigger"]["type"]])
        self.key = f"{event['id']}|{when.strftime('%Y-%m-%dT%H:%M')}"
        self.suppressed_by = None


def _active_on(event, day):
    start, end = event.get("active_from"), event.get("active_until")
    if start and day < date.fromisoformat(start):
        return False
    if end and day > date.fromisoformat(end):
        return False
    return True


def _yearly_matches(trigger, day):
    month, dom = int(trigger["month"]), int(trigger["day"])
    # Feb 29 birthdays fire on Feb 28 in non-leap years.
    if month == 2 and dom == 29 and not _is_leap(day.year):
        dom = 28
    return day.month == month and day.day == dom


def _is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def occurrences_for_day(config, day):
    out = []
    prayer_times = None
    weekday = WEEKDAYS[day.weekday()]

    for ev in config.events:
        if not ev.get("enabled", True) or not _active_on(ev, day):
            continue

        trig = ev["trigger"]
        ttype = trig["type"]
        when = None

        if ttype == "prayer":
            days = trig.get("days")
            if days and weekday not in days:
                continue
            if prayer_times is None:
                try:
                    prayer_times = get_prayer_times(config, day)
                except Exception as exc:
                    log(f"Prayer time calculation failed for {day}: {exc}")
                    prayer_times = {}
            base = prayer_times.get(trig["prayer"])
            if base is not None:
                when = base + timedelta(minutes=int(trig.get("offset_minutes", 0)))

        elif ttype == "weekly":
            if weekday in trig["days"]:
                when = datetime.combine(day, _parse_hhmm(trig["time"]), tzinfo=config.tz)

        elif ttype == "yearly":
            if _yearly_matches(trig, day):
                when = datetime.combine(day, _parse_hhmm(trig["time"]), tzinfo=config.tz)

        elif ttype == "once":
            dt = datetime.fromisoformat(trig["datetime"])
            dt = dt.replace(tzinfo=config.tz) if dt.tzinfo is None else dt.astimezone(config.tz)
            if dt.date() == day:
                when = dt

        if when is not None:
            out.append(Occurrence(ev, when.replace(second=0, microsecond=0)))

    out.sort(key=lambda o: o.when)
    return out


def apply_conflicts(occurrences, window_minutes, skipped_ids=()):
    """A lower-priority occurrence near a higher-priority one is suppressed."""
    window = timedelta(minutes=window_minutes)
    winners = []
    for occ in sorted(occurrences, key=lambda o: -o.priority):
        occ.suppressed_by = None
        if occ.event["id"] in skipped_ids:
            continue  # a skipped event shouldn't block anything
        blocker = next(
            (w for w in winners
             if w.priority > occ.priority
             and abs(seconds_between(w.when, occ.when)) <= window.total_seconds()),
            None,
        )
        if blocker:
            occ.suppressed_by = blocker.event["id"]
        else:
            winners.append(occ)


def build_window(config, today):
    """Yesterday + today, so an event near midnight still fires after the date rolls."""
    occs = occurrences_for_day(config, today - timedelta(days=1)) \
        + occurrences_for_day(config, today)
    apply_conflicts(occs, config.settings["conflict_window_minutes"], pending_skips())
    return occs


def print_schedule(occs, day, state=None):
    log("")
    log("=" * 60)
    log(f"Schedule for {day}")
    log("=" * 60)
    todays = [o for o in occs if o.when.date() == day]
    if not todays:
        log("(no events today)")
    for o in todays:
        ttype = o.event["trigger"]["type"]
        note = ""
        if o.suppressed_by:
            note = f"  [suppressed by {o.suppressed_by}]"
        elif state is not None and o.key in state.fired:
            note = f"  [{state.fired[o.key]['status']}]"
        log(f"{o.when.strftime('%I:%M %p')}  {o.event['id']:32} ({ttype}){note}")
    log("=" * 60)


# ============================================================
# PLAYBACK
# ============================================================

_playback_lock = threading.Lock()   # one event at a time (avoids Zeroconf pile-ups)
_history_lock = threading.Lock()


def record_history(event_id, device, audio, volume, status, error=None):
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event_id,
        "device": device,
        "audio": audio,
        "volume": volume,
        "status": status,
    }
    if error:
        entry["error"] = str(error)
    try:
        with _history_lock, open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log(f"Could not write history: {exc}")


def trim_history(keep_days):
    """Drop history lines older than keep_days (runs once per day)."""
    if not HISTORY_PATH.exists():
        return
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    try:
        with _history_lock:
            lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
            kept = []
            for line in lines:
                try:
                    if json.loads(line)["ts"] >= cutoff:
                        kept.append(line)
                except (ValueError, KeyError):
                    continue
            if len(kept) != len(lines):
                tmp = Path(str(HISTORY_PATH) + ".tmp")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                os.replace(tmp, HISTORY_PATH)
    except OSError as exc:
        log(f"Could not trim history: {exc}")


def _discover():
    chromecasts, browser = pychromecast.get_chromecasts()
    return {c.name: c for c in chromecasts}, chromecasts, browser


def _cleanup(chromecasts, browser):
    for cast in chromecasts:
        try:
            cast.disconnect()
        except Exception:
            pass
    if browser is not None:
        try:
            pychromecast.discovery.stop_discovery(browser)
        except Exception:
            pass


def _play_on_device(cast, event_id, name, audio, url, volume, max_seconds):
    try:
        log(f"Connecting to {name}...")
        cast.wait(timeout=10)
        cast.set_volume(volume)

        media = cast.media_controller
        ext = os.path.splitext(audio)[1].lower()
        media.play_media(url, MIME_TYPES.get(ext, "audio/mpeg"))
        media.block_until_active(timeout=10)

        log(f"{event_id} playing on {name} at {int(volume * 100)}%")
        record_history(event_id, name, audio, volume, "playing")

        if max_seconds:
            time.sleep(max_seconds)
            media.stop()
            log(f"{event_id} stopped on {name} after {max_seconds}s")
    except Exception as exc:
        log(f"Skipping {name}: {exc}")
        record_history(event_id, name, audio, volume, "error", exc)


def play_event(event, config):
    event_id = event["id"]
    log("")
    log(f"Starting {event_id}")

    targets = []
    for name, audio, volume in resolve_targets(event, config):
        if not config.devices.get(name, {}).get("enabled", True):
            log(f"Device disabled: {name}")
            record_history(event_id, name, audio, volume, "device_disabled")
            continue
        targets.append((name, audio, volume))
    if not targets:
        return

    wanted = {name for name, _, _ in targets}
    max_seconds = event["action"].get("max_seconds")
    base_url = config.settings["audio_base_url"].rstrip("/")

    with _playback_lock:
        discovered, all_casts, browsers = {}, [], []
        try:
            # Discover; if a device is missing, retry once before giving up.
            for attempt in (1, 2):
                found, casts, browser = _discover()
                all_casts.extend(casts)
                browsers.append(browser)
                for name, cast in found.items():
                    discovered.setdefault(name, cast)
                missing = wanted - set(discovered)
                if not missing:
                    break
                if attempt == 1:
                    log(f"Not found yet: {sorted(missing)}; retrying discovery once")
                    time.sleep(3)

            threads = []
            for name, audio, volume in targets:
                cast = discovered.get(name)
                if cast is None:
                    log(f"Device not found: {name}")
                    record_history(event_id, name, audio, volume, "not_found")
                    continue
                url = f"{base_url}/{quote(audio)}"
                t = threading.Thread(
                    target=_play_on_device,
                    args=(cast, event_id, name, audio, url, volume, max_seconds),
                    daemon=True,
                )
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

        except Exception as exc:
            log(f"Cast discovery error: {exc}")
        finally:
            for browser in browsers:
                _cleanup([], browser)
            _cleanup(all_casts, None)


# ============================================================
# STOP / DEVICE PROBE
# ============================================================

def stop_devices(names):
    """Stop the media session on the named devices. Returns {name: status}.
    Does not take the playback lock, so it can interrupt a running max_seconds play."""
    results = {}
    found, casts, browser = {}, [], None
    try:
        found, casts, browser = _discover()
        for name in names:
            cast = found.get(name)
            if cast is None:
                results[name] = "not_found"
                continue
            try:
                cast.wait(timeout=10)
                cast.media_controller.stop()
                results[name] = "stopped"
                log(f"Stopped playback on {name}")
            except Exception as exc:
                results[name] = f"error: {exc}"
    except Exception as exc:
        log(f"Stop failed: {exc}")
        for name in names:
            results.setdefault(name, f"error: {exc}")
    finally:
        _cleanup(casts, browser)
    for name, status in results.items():
        record_history("manual-stop", name, "-", 0, "stopped" if status == "stopped" else status)
    return results


class DeviceMonitor:
    """Tracks which Cast devices answered the last discovery. Never runs during playback."""

    def __init__(self):
        self.devices = {}
        self.last_probe = 0.0
        self._lock = threading.Lock()
        self._running = False
        self.generation = 0   # bumps after every finished probe

    def probe_due(self, mono_now):
        return not self._running and mono_now - self.last_probe >= DEVICE_PROBE_SECONDS

    def start_probe(self, config, mono_now):
        self.last_probe = mono_now
        self._running = True
        threading.Thread(target=self._probe, args=(config,), daemon=True).start()

    def _probe(self, config):
        try:
            if not _playback_lock.acquire(blocking=False):
                return   # playback in progress; try again next cycle
            try:
                found, casts, browser = _discover()
                _cleanup(casts, browser)
            finally:
                _playback_lock.release()
            checked = datetime.now(config.tz).isoformat(timespec="seconds")
            names = set(config.devices) | set(found)
            with self._lock:
                self.devices = {n: {"online": n in found, "checked": checked} for n in sorted(names)}
                self.generation += 1
        except Exception as exc:
            log(f"Device probe failed: {exc}")
        finally:
            self._running = False

    def snapshot(self):
        with self._lock:
            return dict(self.devices)


# ============================================================
# COMMAND SPOOL  (dashboard -> scheduler)
# ============================================================

def safe_audio_name(name):
    """True if name is a plain file name inside AUDIO_DIR with an allowed extension."""
    return (isinstance(name, str) and name == os.path.basename(name)
            and not name.startswith(".") and name.lower().endswith(AUDIO_EXTENSIONS))


def _manual_event(cmd, config):
    """Validate a play_now command and turn it into a regular event dict."""
    audio = cmd.get("audio")
    if not safe_audio_name(audio):
        raise ValueError(f"bad audio file name: {audio!r}")
    if not (AUDIO_DIR / audio).is_file():
        raise ValueError(f"audio file not found: {audio}")
    devices = cmd.get("devices")
    if not isinstance(devices, dict) or not devices:
        raise ValueError("'devices' must be a non-empty object of name -> volume")
    for name, vol in devices.items():
        if not _is_number(vol) or not 0 <= vol <= 1:
            raise ValueError(f"device '{name}': volume must be between 0 and 1")
    action = {"audio": audio, "devices": dict(devices)}
    if cmd.get("max_seconds") is not None:
        if not _is_number(cmd["max_seconds"]) or cmd["max_seconds"] <= 0:
            raise ValueError("'max_seconds' must be a positive number")
        action["max_seconds"] = cmd["max_seconds"]
    return {"id": f"manual:{cmd.get('id', 'play')}", "trigger": {"type": "once"}, "action": action}


def _command_age_seconds(cmd, path, now):
    created = cmd.get("created")
    if created:
        try:
            ts = datetime.fromisoformat(created)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            return seconds_between(ts, now)
        except (TypeError, ValueError):
            pass
    return time.time() - path.stat().st_mtime


def execute_command(cmd, config):
    """Run one command. Returns a result dict; playback itself runs in a thread."""
    action = cmd.get("action")
    if action == "play_now":
        event = _manual_event(cmd, config)
        threading.Thread(target=play_event, args=(event, config), daemon=True).start()
        return {"status": "started", "event": event["id"]}
    if action == "play_event":
        eid = cmd.get("event_id")
        event = next((e for e in config.events if e["id"] == eid), None)
        if event is None:
            raise ValueError(f"unknown event id: {eid!r}")
        threading.Thread(target=play_event, args=(event, config), daemon=True).start()
        return {"status": "started", "event": eid}
    if action == "stop":
        names = cmd.get("devices")
        if not names:
            names = list(config.devices)
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("'devices' must be a list of device names")
        threading.Thread(target=stop_devices, args=(names,), daemon=True).start()
        return {"status": "started", "devices": names}
    raise ValueError(f"unknown action {action!r}; use one of {list(COMMAND_ACTIONS)}")


def process_commands(config, now):
    """Execute commands/*.json oldest first, then move each to commands/done/."""
    if not COMMANDS_DIR.is_dir():
        return []
    done_dir = COMMANDS_DIR / "done"
    done_dir.mkdir(exist_ok=True)
    results = []

    for path in sorted(COMMANDS_DIR.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name)):
        processed = datetime.now(config.tz).isoformat(timespec="seconds")
        try:
            cmd = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cmd, dict):
                raise ValueError("command must be a JSON object")
        except (OSError, ValueError) as exc:
            cmd, result = {"raw": path.name}, {"status": "error", "message": f"unreadable: {exc}"}
        else:
            age = _command_age_seconds(cmd, path, now)
            if age > COMMAND_MAX_AGE_SECONDS:
                result = {"status": "expired",
                          "message": f"command is {int(age)} s old (max {COMMAND_MAX_AGE_SECONDS} s)"}
            else:
                try:
                    result = execute_command(cmd, config)
                except Exception as exc:
                    result = {"status": "error", "message": str(exc)}
        result["processed"] = processed
        log(f"Command {cmd.get('action', '?')} {cmd.get('id', path.stem)}: {result['status']}"
            + (f" ({result['message']})" if result.get("message") else ""))
        cmd["result"] = result
        try:
            write_json_atomic(done_dir / path.name, cmd)
            path.unlink()
        except OSError as exc:
            log(f"Could not archive command {path.name}: {exc}")
            try:
                path.unlink()   # never execute the same command twice
            except OSError:
                pass
        results.append(cmd)
    return results


def cleanup_done_commands(keep_days=COMMAND_DONE_KEEP_DAYS):
    done_dir = COMMANDS_DIR / "done"
    if not done_dir.is_dir():
        return
    cutoff = time.time() - keep_days * 86400
    for path in done_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


# ============================================================
# HEARTBEAT
# ============================================================

def upcoming(config, now, state=None, limit=5):
    """Next occurrences that will actually play (today + tomorrow)."""
    out = []
    for day in (now.date(), now.date() + timedelta(days=1)):
        occs = occurrences_for_day(config, day)
        apply_conflicts(occs, config.settings["conflict_window_minutes"], pending_skips())
        for o in occs:
            if o.suppressed_by or seconds_between(now, o.when) <= 0:
                continue
            if state is not None and o.key in state.fired:
                continue
            out.append(o)
    out.sort(key=lambda o: o.when)
    return out[:limit]


def write_status(config, now, state, monitor, last_error):
    status = {
        "updated": now.isoformat(timespec="seconds"),
        "version": VERSION,
        "pid": os.getpid(),
        "next": [{"event": o.event["id"], "when": o.when.isoformat()} for o in upcoming(config, now, state)],
        "devices": monitor.snapshot() if monitor else {},
        "last_error": last_error,
    }
    try:
        write_json_atomic(STATUS_PATH, status)
    except OSError as exc:
        log(f"Could not write status: {exc}")
    return status


# ============================================================
# MAIN LOOP
# ============================================================

def seconds_between(earlier, later):
    """Real elapsed seconds. Subtracting two aware datetimes that share a tzinfo
    ignores DST offsets in Python, so compare in UTC instead."""
    return (later.astimezone(timezone.utc) - earlier.astimezone(timezone.utc)).total_seconds()


def process_due(now, occurrences, config, state, reported):
    grace = config.settings["grace_seconds"]

    for occ in occurrences:
        delta = seconds_between(occ.when, now)
        if delta < 0 or occ.key in state.fired:
            continue

        if delta > grace:
            if occ.key not in reported:
                reported.add(occ.key)
                if delta <= 1800:
                    log(f"Missed {occ.event['id']} scheduled "
                        f"{occ.when.strftime('%H:%M')} (outside {grace}s grace window)")
            continue

        event_id = occ.event["id"]

        if occ.suppressed_by:
            state.mark(occ.key, "suppressed")
            log(f"{event_id} suppressed (yielded to {occ.suppressed_by})")
            record_history(event_id, "-", "-", 0, "suppressed")
            continue

        if consume_skip(event_id):
            state.mark(occ.key, "skipped")
            log(f"{event_id} skipped (skip_next requested)")
            record_history(event_id, "-", "-", 0, "skipped")
            continue

        # Mark BEFORE playing: at-most-once, so a crash mid-play can't replay it.
        state.mark(occ.key, "fired")
        log(f"{now.strftime('%Y-%m-%d %H:%M:%S')} - Triggering {event_id}")
        threading.Thread(
            target=play_event, args=(occ.event, config), daemon=True
        ).start()


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def main():
    log(f"Nidaa scheduler started. (version {VERSION})")

    try:
        config, problems = load_config()
    except Exception as exc:
        log(f"FATAL: cannot load {CONFIG_PATH}: {exc}")
        sys.exit(1)
    for problem in problems:
        log(f"Config problem (event ignored): {problem}")

    state = State()
    config_mtime = CONFIG_PATH.stat().st_mtime
    skips_mtime = _mtime(SKIPS_PATH)
    current_day = None
    occurrences = []
    reported = set()
    rebuild = True
    monitor = DeviceMonitor()
    last_heartbeat = 0.0
    heartbeat_generation = 0
    last_error = None
    COMMANDS_DIR.mkdir(exist_ok=True)

    while True:
        try:
            now = datetime.now(config.tz)
            today = now.date()

            # Hot-reload config when the file changes.
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime != config_mtime:
                    config_mtime = mtime
                    new_config, problems = load_config()
                    config = new_config
                    rebuild = True
                    log("Config reloaded.")
                    for problem in problems:
                        log(f"Config problem (event ignored): {problem}")
            except Exception as exc:
                log(f"Config reload failed, keeping previous config: {exc}")

            # A new skip request can release a conflict, so rebuild on change.
            mtime = _mtime(SKIPS_PATH)
            if mtime != skips_mtime:
                skips_mtime = mtime
                rebuild = True

            if current_day != today:
                current_day = today
                rebuild = True
                state.prune()
                trim_history(config.settings["history_days"])
                cleanup_done_commands()

            if rebuild:
                occurrences = build_window(config, today)
                print_schedule(occurrences, today, state)
                rebuild = False

            process_due(now, occurrences, config, state, reported)
            process_commands(config, now)

            mono = time.monotonic()
            if monitor.probe_due(mono):
                monitor.start_probe(config, mono)
            if mono - last_heartbeat >= HEARTBEAT_SECONDS or monitor.generation != heartbeat_generation:
                last_heartbeat = mono
                heartbeat_generation = monitor.generation
                write_status(config, now, state, monitor, last_error)

        except Exception as exc:
            # Never let an unexpected error kill the long-running process.
            last_error = f"{datetime.now().isoformat(timespec='seconds')} {exc}"
            log(f"Scheduler error: {exc}")

        time.sleep(LOOP_SECONDS)


# ============================================================
# CLI
# ============================================================

def cli():
    parser = argparse.ArgumentParser(description="Nidaa scheduler")
    parser.add_argument("--check", action="store_true", help="validate config.json")
    parser.add_argument("--today", action="store_true", help="print today's schedule")
    parser.add_argument("--skip", metavar="EVENT_ID", help="skip next occurrence of an event")
    parser.add_argument("--play", metavar="EVENT_ID", help="play an event now (test)")
    args = parser.parse_args()

    if not (args.check or args.today or args.skip or args.play):
        main()
        return

    try:
        config, problems = load_config()
    except Exception as exc:
        print(f"Config error: {exc}")
        sys.exit(1)

    ids = {e["id"] for e in config.events}

    if args.check:
        print(f"{len(config.events)} valid event(s), {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        if AUDIO_DIR.is_dir():
            for audio, ids in sorted(referenced_audio(config).items()):
                if not (AUDIO_DIR / audio).is_file():
                    print(f"  ! warning: {audio} (used by {', '.join(ids)}) not in {AUDIO_DIR}")
        sys.exit(1 if problems else 0)

    if args.today:
        today = datetime.now(config.tz).date()
        print_schedule(build_window(config, today), today, State())
        return

    if args.skip:
        if args.skip not in ids:
            print(f"Unknown event id: {args.skip}. Known: {sorted(ids)}")
            sys.exit(1)
        add_skip(args.skip)
        print(f"Next occurrence of {args.skip} will be skipped.")
        return

    if args.play:
        event = next((e for e in config.events if e["id"] == args.play), None)
        if event is None:
            print(f"Unknown event id: {args.play}. Known: {sorted(ids)}")
            sys.exit(1)
        play_event(event, config)


if __name__ == "__main__":
    cli()
