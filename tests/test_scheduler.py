"""Phase 1 tests for the v2 event model (plan section 1.1)."""

import copy
import json
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import nidaa_scheduler as sched
from conftest import PRAYER_OVERRIDES, PRAYER_CALLS, SAMPLE_CONFIG, CASTS, FakeCast

TZ = ZoneInfo("America/New_York")


def make_config(events, **settings):
    raw = {"settings": {**SAMPLE_CONFIG["settings"], **settings},
           "devices": copy.deepcopy(SAMPLE_CONFIG["devices"]),
           "events": events}
    config, problems = sched.config_from_dict(raw)
    assert problems == []
    return config


def ev(eid, trigger, devices=None, **extra):
    return {"id": eid, "enabled": True, "trigger": trigger,
            "action": {"audio": "sound-test.mp3", "devices": devices or {"Kitchen speaker": 0.3}},
            **extra}


def occ_times(config, day):
    return {o.event["id"]: o.when for o in sched.occurrences_for_day(config, day)}


# ------------------------------------------------------------------ the shipped config

def test_shipped_config_is_valid(workdir):
    config, problems = sched.load_config()
    assert problems == []
    assert len(config.events) == 8
    assert set(config.devices) == {"Bedroom display", "Living Room display",
                                   "Kitchen speaker"}
    # Every audio file the config references exists in the real audio folder.
    audio_dir = sched.BASE_DIR.parent / "audio"
    for audio in sched.referenced_audio(config, enabled_only=False):
        assert (audio_dir / audio).is_file(), audio


# ------------------------------------------------------------------ trigger types

def test_prayer_trigger_and_offset():
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("suhoor", {"type": "prayer", "prayer": "Fajr", "offset_minutes": -45}),
        ev("after-isha", {"type": "prayer", "prayer": "Isha", "offset_minutes": 10}),
    ])
    t = occ_times(config, date(2026, 5, 12))
    assert t["fajr"] == datetime(2026, 5, 12, 5, 30, tzinfo=TZ)
    assert t["suhoor"] == datetime(2026, 5, 12, 4, 45, tzinfo=TZ)
    assert t["after-isha"] == datetime(2026, 5, 12, 19, 40, tzinfo=TZ)


def test_prayer_days_filter():
    config = make_config([ev("jumuah", {"type": "prayer", "prayer": "Dhuhr", "days": ["fri"]})])
    assert "jumuah" in occ_times(config, date(2026, 11, 20))      # Friday
    assert "jumuah" not in occ_times(config, date(2026, 11, 19))  # Thursday


def test_weekly_trigger():
    config = make_config([ev("w", {"type": "weekly", "days": ["mon", "wed"], "time": "07:15"})])
    assert occ_times(config, date(2026, 11, 16))["w"] == datetime(2026, 11, 16, 7, 15, tzinfo=TZ)
    assert "w" in occ_times(config, date(2026, 11, 18))
    assert "w" not in occ_times(config, date(2026, 11, 17))


def test_yearly_trigger():
    config = make_config([ev("bday", {"type": "yearly", "month": 6, "day": 15, "time": "08:00"})])
    assert occ_times(config, date(2027, 6, 15))["bday"] == datetime(2027, 6, 15, 8, 0, tzinfo=TZ)
    assert "bday" not in occ_times(config, date(2027, 6, 16))


def test_yearly_feb29_leap_and_non_leap():
    config = make_config([ev("leap", {"type": "yearly", "month": 2, "day": 29, "time": "09:00"})])
    assert "leap" in occ_times(config, date(2028, 2, 29))       # leap year: on the 29th
    assert "leap" not in occ_times(config, date(2028, 2, 28))   # ...and not also on the 28th
    assert "leap" in occ_times(config, date(2027, 2, 28))       # non-leap: falls back to 28th
    assert "leap" not in occ_times(config, date(2027, 3, 1))


def test_once_naive_and_aware():
    config = make_config([
        ev("naive", {"type": "once", "datetime": "2026-12-31T20:00"}),
        # 01:00 UTC on Jan 1 == 20:00 EST on Dec 31
        ev("aware", {"type": "once", "datetime": "2027-01-01T01:00:00+00:00"}),
    ])
    t = occ_times(config, date(2026, 12, 31))
    assert t["naive"] == datetime(2026, 12, 31, 20, 0, tzinfo=TZ)
    assert t["aware"] == datetime(2026, 12, 31, 20, 0, tzinfo=TZ)
    assert occ_times(config, date(2027, 1, 1)) == {}


def test_disabled_and_active_window():
    config = make_config([
        ev("off", {"type": "weekly", "days": sched.WEEKDAYS, "time": "07:00"}, enabled=False),
        ev("ramadan", {"type": "weekly", "days": sched.WEEKDAYS, "time": "04:00"},
           active_from="2027-02-08", active_until="2027-03-09"),
    ])
    assert occ_times(config, date(2027, 2, 7)) == {}
    assert set(occ_times(config, date(2027, 2, 8))) == {"ramadan"}
    assert set(occ_times(config, date(2027, 3, 9))) == {"ramadan"}
    assert occ_times(config, date(2027, 3, 10)) == {}


# ------------------------------------------------------------------ DST

@pytest.mark.parametrize("day,offset_hours", [
    (date(2026, 3, 7), -5), (date(2026, 3, 8), -4),    # spring forward on Mar 8 2026
    (date(2026, 10, 31), -4), (date(2026, 11, 1), -5),  # fall back on Nov 1 2026
])
def test_dst_offsets(day, offset_hours):
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("w", {"type": "weekly", "days": sched.WEEKDAYS, "time": "07:00"}),
    ])
    for when in occ_times(config, day).values():
        assert when.utcoffset() == timedelta(hours=offset_hours)


def test_grace_uses_real_elapsed_time_across_fall_back(workdir):
    """01:30 happens twice on Nov 1 2026. An event at the first 01:30 (EDT) must
    not be fired as 'on time' at the second 01:30 (EST), an hour later."""
    config = make_config([ev("night", {"type": "once", "datetime": "2026-11-01T01:30"})])
    occs = sched.build_window(config, date(2026, 11, 1))
    second_0130 = datetime(2026, 11, 1, 1, 30, 30, tzinfo=TZ, fold=1)
    state = sched.State()
    sched.process_due(second_0130, occs, config, state, set())
    assert state.fired == {}   # 3630 s late, outside grace


def test_seconds_between_handles_dst():
    a = datetime(2026, 3, 8, 1, 59, tzinfo=TZ)
    b = datetime(2026, 3, 8, 3, 1, tzinfo=TZ)
    assert sched.seconds_between(a, b) == 120


# ------------------------------------------------------------------ midnight rollover

def test_midnight_rollover_window(workdir, monkeypatch):
    config = make_config([ev("late", {"type": "weekly", "days": ["wed"], "time": "23:59"})])
    fired = []
    monkeypatch.setattr(sched, "play_event", lambda e, c: fired.append(e["id"]))
    # It is now 00:00:40 on Thursday; the window must still contain Wednesday 23:59.
    now = datetime(2026, 11, 19, 0, 0, 40, tzinfo=TZ)
    occs = sched.build_window(config, now.date())
    assert [o.event["id"] for o in occs] == ["late"]
    state = sched.State()
    sched.process_due(now, occs, config, state, set())
    _join_threads()
    assert fired == ["late"]


# ------------------------------------------------------------------ conflicts

def test_conflict_weekly_suppressed_by_prayer():
    PRAYER_OVERRIDES[date(2026, 11, 18)] = {"fajr": dtime(7, 2)}
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("w", {"type": "weekly", "days": ["wed"], "time": "07:00"}),
    ])
    occs = sched.occurrences_for_day(config, date(2026, 11, 18))
    sched.apply_conflicts(occs, 3)
    by_id = {o.event["id"]: o for o in occs}
    assert by_id["w"].suppressed_by == "fajr"
    assert by_id["fajr"].suppressed_by is None


def test_conflict_outside_window_not_suppressed():
    PRAYER_OVERRIDES[date(2026, 11, 18)] = {"fajr": dtime(7, 4)}
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("w", {"type": "weekly", "days": ["wed"], "time": "07:00"}),
    ])
    occs = sched.occurrences_for_day(config, date(2026, 11, 18))
    sched.apply_conflicts(occs, 3)
    assert all(o.suppressed_by is None for o in occs)


def test_skipped_prayer_does_not_block():
    PRAYER_OVERRIDES[date(2026, 11, 18)] = {"fajr": dtime(7, 2)}
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("w", {"type": "weekly", "days": ["wed"], "time": "07:00"}),
    ])
    occs = sched.occurrences_for_day(config, date(2026, 11, 18))
    sched.apply_conflicts(occs, 3, skipped_ids={"fajr"})
    assert all(o.suppressed_by is None for o in occs)


def test_explicit_priority_wins():
    PRAYER_OVERRIDES[date(2026, 11, 18)] = {"fajr": dtime(7, 2)}
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("urgent", {"type": "weekly", "days": ["wed"], "time": "07:00"}, priority=200),
    ])
    occs = sched.occurrences_for_day(config, date(2026, 11, 18))
    sched.apply_conflicts(occs, 3)
    assert {o.event["id"]: o.suppressed_by for o in occs} == {"fajr": "urgent", "urgent": None}


def test_suppressed_occurrence_is_recorded_not_played(workdir, monkeypatch):
    PRAYER_OVERRIDES[date(2026, 11, 18)] = {"fajr": dtime(7, 2)}
    config = make_config([
        ev("fajr", {"type": "prayer", "prayer": "Fajr"}),
        ev("w", {"type": "weekly", "days": ["wed"], "time": "07:00"}),
    ])
    played = []
    monkeypatch.setattr(sched, "play_event", lambda e, c: played.append(e["id"]))
    occs = sched.build_window(config, date(2026, 11, 18))
    state = sched.State()
    sched.process_due(datetime(2026, 11, 18, 7, 0, 5, tzinfo=TZ), occs, config, state, set())
    _join_threads()
    assert played == []
    assert state.fired["w|2026-11-18T07:00"]["status"] == "suppressed"


# ------------------------------------------------------------------ grace window + state

def _join_threads():
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=5)


def _weekly_config():
    return make_config([ev("w", {"type": "weekly", "days": ["wed"], "time": "07:00"})])


@pytest.mark.parametrize("late_seconds,should_fire", [(0, True), (60, True), (120, True),
                                                      (300, False), (-30, False)])
def test_grace_window(workdir, monkeypatch, late_seconds, should_fire):
    config = _weekly_config()
    played = []
    monkeypatch.setattr(sched, "play_event", lambda e, c: played.append(e["id"]))
    occs = sched.build_window(config, date(2026, 11, 18))
    now = datetime(2026, 11, 18, 7, 0, tzinfo=TZ) + timedelta(seconds=late_seconds)
    state = sched.State()
    sched.process_due(now, occs, config, state, set())
    _join_threads()
    assert played == (["w"] if should_fire else [])


def test_missed_is_logged_once(workdir, monkeypatch, capsys):
    config = _weekly_config()
    monkeypatch.setattr(sched, "play_event", lambda e, c: None)
    occs = sched.build_window(config, date(2026, 11, 18))
    state, reported = sched.State(), set()
    now = datetime(2026, 11, 18, 7, 5, tzinfo=TZ)
    sched.process_due(now, occs, config, state, reported)
    sched.process_due(now + timedelta(seconds=5), occs, config, state, reported)
    assert capsys.readouterr().out.count("Missed w") == 1


def test_state_prevents_replay_after_restart(workdir, monkeypatch):
    config = _weekly_config()
    played = []
    monkeypatch.setattr(sched, "play_event", lambda e, c: played.append(e["id"]))
    now = datetime(2026, 11, 18, 7, 0, 10, tzinfo=TZ)

    sched.process_due(now, sched.build_window(config, now.date()), config, sched.State(), set())
    _join_threads()
    assert played == ["w"]
    saved = json.loads(sched.STATE_PATH.read_text())
    assert saved["fired"]["w|2026-11-18T07:00"]["status"] == "fired"

    # "Restart": brand-new State loaded from disk, a few seconds later.
    sched.process_due(now + timedelta(seconds=20), sched.build_window(config, now.date()),
                      config, sched.State(), set())
    _join_threads()
    assert played == ["w"]


def test_state_prune(workdir):
    state = sched.State()
    state.fired = {"old|x": {"at": (datetime.now() - timedelta(days=10)).isoformat(), "status": "fired"},
                   "new|x": {"at": datetime.now().isoformat(), "status": "fired"}}
    state.prune(keep_days=7)
    assert set(sched.State().fired) == {"new|x"}


# ------------------------------------------------------------------ skips

def test_skip_next_consumed_exactly_once(workdir, monkeypatch):
    config = make_config([ev("w", {"type": "weekly", "days": sched.WEEKDAYS, "time": "07:00"})])
    played = []
    monkeypatch.setattr(sched, "play_event", lambda e, c: played.append(e["id"]))
    sched.add_skip("w")
    sched.add_skip("w")   # duplicate request is still one skip
    assert json.loads(sched.SKIPS_PATH.read_text()) == {"skip_next": ["w"]}

    day1 = datetime(2026, 11, 18, 7, 0, 5, tzinfo=TZ)
    state = sched.State()
    sched.process_due(day1, sched.build_window(config, day1.date()), config, state, set())
    _join_threads()
    assert played == []
    assert state.fired["w|2026-11-18T07:00"]["status"] == "skipped"
    assert sched.pending_skips() == set()

    day2 = day1 + timedelta(days=1)
    sched.process_due(day2, sched.build_window(config, day2.date()), config, state, set())
    _join_threads()
    assert played == ["w"]


# ------------------------------------------------------------------ validation

def _errors(event):
    return sched.validate_event(event, set())


@pytest.mark.parametrize("event,needle", [
    (ev("x", {"type": "weekly", "days": ["funday"], "time": "07:00"}), "unknown day"),
    (ev("x", {"type": "weekly", "days": [], "time": "07:00"}), "non-empty"),
    (ev("x", {"type": "weekly", "time": "07:00"}), "'days' is required"),
    (ev("x", {"type": "weekly", "days": ["mon"], "time": "25:00"}), "bad trigger"),
    (ev("x", {"type": "weekly", "days": ["mon"], "time": "7am"}), "bad trigger"),
    (ev("x", {"type": "yearly", "month": 2, "day": 30, "time": "07:00"}), "bad trigger"),
    (ev("x", {"type": "once", "datetime": "tomorrow"}), "bad trigger"),
    (ev("x", {"type": "prayer", "prayer": "Tahajjud"}), "'prayer' must be one of"),
    (ev("x", {"type": "hourly"}), "unknown trigger type"),
    ({"id": "x", "action": {"audio": "sound-test.mp3", "devices": {"d": 0.2}}}, "missing 'trigger'"),
    ({"trigger": {"type": "once", "datetime": "2026-01-01T00:00"},
      "action": {"audio": "sound-test.mp3", "devices": {"d": 0.2}}}, "missing 'id'"),
    ({"id": "x", "trigger": {"type": "once", "datetime": "2026-01-01T00:00"},
      "action": {"devices": {"d": 0.2}}}, "no audio"),
    ({"id": "x", "trigger": {"type": "once", "datetime": "2026-01-01T00:00"},
      "action": {"audio": "sound-test.mp3", "devices": {}}}, "non-empty object"),
    (ev("x", {"type": "once", "datetime": "2026-01-01T00:00"}, {"d": 1.5}), "between 0 and 1"),
    (ev("x", {"type": "once", "datetime": "2026-01-01T00:00"}, {"d": {"volume": -0.1}}), "between 0 and 1"),
    (ev("x", {"type": "once", "datetime": "2026-01-01T00:00"}, {"d": True}), "volume number"),
    (ev("x", {"type": "once", "datetime": "2026-01-01T00:00"}, active_from="next week"), "YYYY-MM-DD"),
    (ev("x", {"type": "once", "datetime": "2026-01-01T00:00"}, priority="high"), "integer"),
])
def test_validation_rejects(event, needle):
    errors = _errors(event)
    assert errors and any(needle in e for e in errors), errors


def test_validation_max_seconds():
    bad = ev("x", {"type": "once", "datetime": "2026-01-01T00:00"})
    bad["action"]["max_seconds"] = 0
    assert any("max_seconds" in e for e in _errors(bad))


def test_duplicate_id_and_bad_event_skipped(workdir):
    raw = copy.deepcopy(SAMPLE_CONFIG)
    raw["events"].append(copy.deepcopy(raw["events"][0]))                       # duplicate id
    raw["events"].append(ev("bad", {"type": "weekly", "days": ["funday"], "time": "07:00"}))
    config, problems = sched.config_from_dict(raw)
    assert len(config.events) == 8
    assert any("duplicate" in p for p in problems)
    assert any(p.startswith("bad:") for p in problems)


def test_per_device_audio_override_is_valid():
    e = ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
           {"d1": 0.2, "d2": {"audio": "sound-test-alt.mp3", "volume": 0.4}})
    assert _errors(e) == []


def test_bad_settings_raise():
    raw = copy.deepcopy(SAMPLE_CONFIG)
    raw["settings"]["calculation_method"] = "NOPE"
    with pytest.raises(ValueError):
        sched.config_from_dict(raw)
    raw = copy.deepcopy(SAMPLE_CONFIG)
    raw["settings"]["max_volume"] = 3
    with pytest.raises(ValueError):
        sched.config_from_dict(raw)


def test_invalid_json_raises(workdir):
    sched.CONFIG_PATH.write_text('{"events": [ }')
    with pytest.raises(ValueError):
        sched.load_config()


# ------------------------------------------------------------------ volume cap / targets

def test_volume_cap_applied():
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"a": 0.9, "b": {"volume": 0.1}, "c": {"audio": "sound-test-alt.mp3"}})],
                         max_volume=0.5, default_volume=0.35)
    assert sched.resolve_targets(config.events[0], config) == [
        ("a", "sound-test.mp3", 0.5), ("b", "sound-test.mp3", 0.1), ("c", "sound-test-alt.mp3", 0.35)]


def test_madhab_setting_passes_parameters():
    config = make_config([ev("asr", {"type": "prayer", "prayer": "Asr"})], madhab="HANAFI")
    sched.get_prayer_times(config, date(2026, 11, 18))
    params = PRAYER_CALLS[-1]["params"]
    assert params is not None and params.madhab.name == "HANAFI"

    config = make_config([ev("asr", {"type": "prayer", "prayer": "Asr"})])
    sched.get_prayer_times(config, date(2026, 11, 18))
    assert PRAYER_CALLS[-1]["params"] is None   # default path unchanged from v1


# ------------------------------------------------------------------ playback (fake casts)

def _history():
    if not sched.HISTORY_PATH.exists():
        return []
    return [json.loads(l) for l in sched.HISTORY_PATH.read_text().splitlines()]


def test_play_event_parallel_with_cap(workdir, casts, no_sleep):
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"Kitchen speaker": 0.9, "Living Room display": 0.2})],
                         max_volume=0.5)
    sched.play_event(config.events[0], config)
    assert casts["Kitchen speaker"].volume == 0.5
    assert casts["Living Room display"].volume == 0.2
    assert casts["Bedroom display"].calls == []
    play = [c for c in casts["Kitchen speaker"].calls if c[0] == "play"][0]
    assert play[1] == "http://192.168.1.50:8080/audio/sound-test.mp3" and play[2] == "audio/mpeg"
    assert sorted(h["status"] for h in _history()) == ["playing", "playing"]


def test_play_event_url_quotes_filename(workdir, casts, no_sleep):
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"Kitchen speaker": {"audio": "sound test.m4a", "volume": 0.2}})])
    sched.play_event(config.events[0], config)
    play = [c for c in casts["Kitchen speaker"].calls if c[0] == "play"][0]
    assert play[1].endswith("/sound%20test.m4a") and play[2] == "audio/mp4"


def test_max_seconds_stops(workdir, casts, no_sleep):
    e = ev("x", {"type": "once", "datetime": "2026-01-01T00:00"})
    e["action"]["max_seconds"] = 8
    config = make_config([e])
    sched.play_event(config.events[0], config)
    assert casts["Kitchen speaker"].calls[-1] == ("stop",)


def test_device_not_found_retries_once(workdir, casts, no_sleep):
    from conftest import DISCOVERY_CALLS
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"Kitchen speaker": 0.2, "Ghost speaker": 0.2})])
    sched.play_event(config.events[0], config)
    assert len(DISCOVERY_CALLS) == 2
    statuses = {h["device"]: h["status"] for h in _history()}
    assert statuses == {"Kitchen speaker": "playing", "Ghost speaker": "not_found"}


def test_disabled_device(workdir, casts, no_sleep):
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"Kitchen speaker": 0.2, "Living Room display": 0.2})])
    config.devices["Living Room display"]["enabled"] = False
    sched.play_event(config.events[0], config)
    assert casts["Living Room display"].calls == []
    statuses = {h["device"]: h["status"] for h in _history()}
    assert statuses["Living Room display"] == "device_disabled"


def test_device_error_does_not_stop_others(workdir, casts, no_sleep):
    def boom(v):
        raise RuntimeError("socket closed")
    casts["Kitchen speaker"].set_volume = boom
    config = make_config([ev("x", {"type": "once", "datetime": "2026-01-01T00:00"},
                             {"Kitchen speaker": 0.2, "Living Room display": 0.2})])
    sched.play_event(config.events[0], config)
    statuses = {h["device"]: h["status"] for h in _history()}
    assert statuses == {"Kitchen speaker": "error", "Living Room display": "playing"}


def test_trim_history(workdir):
    old = (datetime.now() - timedelta(days=100)).isoformat(timespec="seconds")
    new = datetime.now().isoformat(timespec="seconds")
    sched.HISTORY_PATH.write_text(json.dumps({"ts": old}) + "\n" + json.dumps({"ts": new}) + "\nnot json\n")
    sched.trim_history(90)
    assert [h["ts"] for h in _history()] == [new]
