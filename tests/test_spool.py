"""Phase 2.1 tests: command spool, status heartbeat, stop, device probe."""

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import nidaa_scheduler as sched
from test_scheduler import _join_threads, _history

TZ = ZoneInfo("America/New_York")


def _cmd(workdir, name, data, age_seconds=0):
    d = sched.COMMANDS_DIR
    d.mkdir(exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(json.dumps(data))
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


def _done(name):
    return json.loads((sched.COMMANDS_DIR / "done" / f"{name}.json").read_text())


def _cfg():
    config, problems = sched.load_config()
    assert not problems
    return config


def test_play_now_plays_with_cap(workdir, casts, no_sleep):
    config = _cfg()   # shipped max_volume 0.8
    _cmd(workdir, "c1", {"id": "c1", "action": "play_now", "audio": "sound-test.mp3",
                         "devices": {"Kitchen speaker": 0.95}, "max_seconds": 5})
    sched.process_commands(config, datetime.now(TZ))
    _join_threads()
    assert casts["Kitchen speaker"].volume == 0.8
    assert casts["Kitchen speaker"].calls[-1] == ("stop",)
    assert _done("c1")["result"]["status"] == "started"
    assert not (sched.COMMANDS_DIR / "c1.json").exists()
    assert [h["event"] for h in _history()] == ["manual:c1"]


def test_play_now_rejects_missing_or_unsafe_audio(workdir, casts, no_sleep):
    config = _cfg()
    _cmd(workdir, "a", {"action": "play_now", "audio": "sound-test-missing.mp3", "devices": {"Kitchen speaker": 0.2}})
    _cmd(workdir, "b", {"action": "play_now", "audio": "../app/config.json", "devices": {"x": 0.2}})
    _cmd(workdir, "c", {"action": "play_now", "audio": "sound-test.mp3", "devices": {"x": 2}})
    sched.process_commands(config, datetime.now(TZ))
    _join_threads()
    assert "not found" in _done("a")["result"]["message"]
    assert "bad audio" in _done("b")["result"]["message"]
    assert "between 0 and 1" in _done("c")["result"]["message"]
    assert all(c.calls == [] for c in casts.values())


def test_play_event_command_works_for_disabled_example(workdir, casts, no_sleep):
    config = _cfg()
    _cmd(workdir, "t", {"action": "play_event", "event_id": "example-birthday"})
    _cmd(workdir, "u", {"action": "play_event", "event_id": "no-such-event"})
    sched.process_commands(config, datetime.now(TZ))
    _join_threads()
    assert _done("t")["result"]["status"] == "started"
    assert casts["Kitchen speaker"].volume == 0.3
    assert _done("u")["result"]["status"] == "error"


def test_stop_command(workdir, casts):
    config = _cfg()
    _cmd(workdir, "s", {"action": "stop", "devices": ["Kitchen speaker", "Ghost"]})
    sched.process_commands(config, datetime.now(TZ))
    _join_threads()
    assert casts["Kitchen speaker"].calls == [("stop",)]
    statuses = {h["device"]: h["status"] for h in _history()}
    assert statuses == {"Kitchen speaker": "stopped", "Ghost": "not_found"}


def test_stop_without_devices_stops_all_configured(workdir, casts):
    _cmd(workdir, "s", {"action": "stop"})
    sched.process_commands(_cfg(), datetime.now(TZ))
    _join_threads()
    assert all(c.calls == [("stop",)] for c in casts.values())


def test_old_commands_expire(workdir, casts, no_sleep):
    config = _cfg()
    now = datetime.now(TZ)
    _cmd(workdir, "old-mtime", {"action": "play_now", "audio": "sound-test.mp3",
                                "devices": {"Kitchen speaker": 0.2}}, age_seconds=400)
    _cmd(workdir, "old-created", {"action": "play_now", "audio": "sound-test.mp3",
                                  "devices": {"Kitchen speaker": 0.2},
                                  "created": (now - timedelta(minutes=6)).isoformat()})
    sched.process_commands(config, now)
    _join_threads()
    assert _done("old-mtime")["result"]["status"] == "expired"
    assert _done("old-created")["result"]["status"] == "expired"
    assert casts["Kitchen speaker"].calls == []


def test_commands_processed_oldest_first_and_once(workdir, casts, no_sleep):
    config = _cfg()
    _cmd(workdir, "zzz", {"action": "stop", "devices": ["Kitchen speaker"]}, age_seconds=30)
    _cmd(workdir, "aaa", {"action": "stop", "devices": ["Kitchen speaker"]}, age_seconds=10)
    results = sched.process_commands(config, datetime.now(TZ))
    assert [r["result"]["processed"] is not None for r in results] == [True, True]
    assert [p.name for p in sorted((sched.COMMANDS_DIR / "done").iterdir())] == ["aaa.json", "zzz.json"]
    # Order: zzz (older) first.
    assert results[0]["devices"] == ["Kitchen speaker"] and results[0]["action"] == "stop"
    assert sched.process_commands(config, datetime.now(TZ)) == []
    _join_threads()


def test_bad_json_and_unknown_action(workdir):
    config = _cfg()
    sched.COMMANDS_DIR.mkdir()
    (sched.COMMANDS_DIR / "broken.json").write_text("{not json")
    _cmd(workdir, "weird", {"action": "dance"})
    (sched.COMMANDS_DIR / "partial.json.tmp").write_text("{")   # in-progress write is ignored
    sched.process_commands(config, datetime.now(TZ))
    assert _done("broken")["result"]["status"] == "error"
    assert "unknown action" in _done("weird")["result"]["message"]
    assert (sched.COMMANDS_DIR / "partial.json.tmp").exists()


def test_cleanup_done_commands(workdir):
    done = sched.COMMANDS_DIR / "done"
    done.mkdir(parents=True)
    old, new = done / "old.json", done / "new.json"
    old.write_text("{}"), new.write_text("{}")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    sched.cleanup_done_commands()
    assert not old.exists() and new.exists()


def test_status_heartbeat(workdir):
    config = _cfg()
    now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)   # stub prayers: 05:30 12:30 15:30 18:00 19:30
    monitor = sched.DeviceMonitor()
    monitor.devices = {"Kitchen speaker": {"online": True, "checked": now.isoformat()}}
    sched.write_status(config, now, sched.State(), monitor, None)
    status = json.loads(sched.STATUS_PATH.read_text())
    assert status["version"] == sched.VERSION and status["pid"] == os.getpid()
    assert [n["event"] for n in status["next"]] == [
        "prayer-dhuhr", "prayer-asr", "prayer-maghrib", "prayer-isha", "prayer-fajr"]
    assert status["next"][-1]["when"].startswith("2026-11-19T05:30")
    assert status["devices"]["Kitchen speaker"]["online"] is True
    assert status["last_error"] is None


def test_upcoming_skips_fired_and_skipped(workdir):
    config = _cfg()
    now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
    state = sched.State()
    state.mark("prayer-dhuhr|2026-11-18T12:30", "fired")
    ids = [o.event["id"] for o in sched.upcoming(config, now, state)]
    assert ids[0] == "prayer-asr"


def test_device_probe(workdir, casts):
    config = _cfg()
    del casts["Living Room display"]
    monitor = sched.DeviceMonitor()
    assert monitor.probe_due(time.monotonic() + 1000)
    monitor._probe(config)
    snap = monitor.snapshot()
    assert snap["Kitchen speaker"]["online"] is True
    assert snap["Living Room display"]["online"] is False


def test_device_probe_skips_during_playback(workdir, casts):
    from conftest import DISCOVERY_CALLS
    config = _cfg()
    monitor = sched.DeviceMonitor()
    with sched._playback_lock:
        monitor._probe(config)
    assert DISCOVERY_CALLS == [] and monitor.snapshot() == {}
