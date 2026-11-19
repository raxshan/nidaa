"""Phase 2 dashboard tests (plan section 2.7), run against a temp directory."""

import io
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import nidaa_scheduler as sched

PASSWORD = "correct horse battery staple"
TOKEN = "test-api-token-123"
TZ = ZoneInfo("America/New_York")
MP3 = b"ID3\x04\x00" + b"\x00" * 200


@pytest.fixture
def dash(workdir, monkeypatch):
    monkeypatch.setenv("NIDAA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("NIDAA_API_TOKEN", TOKEN)
    monkeypatch.setenv("NIDAA_SESSION_SECRET", "s" * 32)
    import nidaa_dashboard
    import nidaa_store
    return nidaa_dashboard, nidaa_store


@pytest.fixture
def anon(dash):
    return TestClient(dash[0].create_app(), follow_redirects=False)


@pytest.fixture
def client(anon):
    r = anon.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 303
    page = anon.get("/").text
    anon.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return anon


@pytest.fixture
def api(dash):
    c = TestClient(dash[0].create_app(), follow_redirects=False)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def raw_config():
    return json.loads(sched.CONFIG_PATH.read_text())


def version(store):
    return store.current_version()


def form_for_weekly(csrf, ver, eid="kids-quran", **over):
    data = {"csrf_token": csrf, "version": ver, "id": eid, "name": "Kids Quran", "enabled": "on",
            "trigger_type": "weekly", "days": ["sat", "sun"], "time": "09:30",
            "audio": "sound-test.mp3", "max_seconds": "120",
            "dev_0_name": "Bedroom display", "dev_0_vol": "30",
            "dev_1_name": "Living Room display", "dev_1_vol": "30",
            "dev_2_name": "Kitchen speaker", "dev_2_on": "on", "dev_2_vol": "40"}
    data.update(over)
    return data


# ------------------------------------------------------------------ auth

UI_PAGES = ["/", "/events", "/events/new", "/prayers", "/history", "/library", "/play",
            "/settings", "/backups", "/events/prayer-fajr/edit"]
API_GETS = ["/api/status", "/api/schedule", "/api/events", "/api/events/prayer-fajr", "/api/devices",
            "/api/history", "/api/audio", "/api/settings", "/api/config/backups"]


@pytest.mark.parametrize("path", UI_PAGES)
def test_ui_requires_login(anon, path):
    r = anon.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", API_GETS)
def test_api_requires_auth(anon, path):
    assert anon.get(path).status_code == 401
    assert anon.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("post", "/api/events"), ("put", "/api/events/prayer-fajr"), ("delete", "/api/events/prayer-fajr"),
    ("post", "/api/play"), ("post", "/api/stop"), ("put", "/api/settings"), ("post", "/api/config/restore"),
    ("post", "/api/events/prayer-fajr/skip"), ("post", "/api/events/prayer-fajr/test"),
    ("delete", "/api/audio/sound-test.mp3"), ("post", "/api/audio"),
])
def test_api_writes_require_auth(anon, method, path):
    assert getattr(anon, method)(path).status_code == 401


@pytest.mark.parametrize("path", ["/events/save", "/prayers", "/settings", "/play", "/stop",
                                  "/library/upload", "/backups/restore", "/events/prayer-fajr/skip"])
def test_ui_posts_require_login(anon, path):
    r = anon.post(path, data={})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", UI_PAGES)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "Nidaa" in r.text


def test_wrong_password_and_rate_limit(anon):
    for _ in range(5):
        assert anon.post("/login", data={"password": "nope"}).status_code == 401
    r = anon.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429 and "Too many attempts" in r.text


def test_lockout_expires(dash, monkeypatch):
    app = dash[0].create_app()
    app.state.limiter.lockout = 0
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        c.post("/login", data={"password": "nope"})
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303


def test_no_password_configured_means_no_login(workdir, monkeypatch):
    monkeypatch.delenv("NIDAA_DASHBOARD_PASSWORD", raising=False)
    import nidaa_dashboard
    c = TestClient(nidaa_dashboard.create_app(), follow_redirects=False)
    assert "No dashboard password is set" in c.get("/login").text
    assert c.post("/login", data={"password": ""}).status_code == 401


def test_open_redirect_blocked(anon):
    r = anon.post("/login", data={"password": PASSWORD, "next": "//evil.example"})
    assert r.headers["location"] == "/"


def test_session_cookie_flags(anon):
    r = anon.post("/login", data={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_csrf_required_on_ui_posts(client, dash):
    store = dash[1]
    data = form_for_weekly("bad-token", version(store))
    assert client.post("/events/save", data=data).status_code == 403
    data["csrf_token"] = ""
    assert client.post("/events/save", data=data).status_code == 403
    assert "kids-quran" not in sched.CONFIG_PATH.read_text()


def test_csrf_required_on_api_with_session(client):
    body = {"audio": "sound-test.mp3", "devices": {"Kitchen speaker": 0.2}}
    assert client.post("/api/play", json=body).status_code == 403
    r = client.post("/api/play", json=body, headers={"X-CSRF-Token": client.csrf})
    assert r.status_code == 202


def test_logout(client):
    client.post("/logout", data={"csrf_token": client.csrf})
    assert client.get("/").status_code == 303


# ------------------------------------------------------------------ editing via the UI

def test_create_weekly_event_from_form(client, dash):
    store = dash[1]
    r = client.post("/events/save", data=form_for_weekly(client.csrf, version(store)))
    assert r.status_code == 303, r.text
    ev = next(e for e in raw_config()["events"] if e["id"] == "kids-quran")
    assert ev == {"id": "kids-quran", "name": "Kids Quran", "enabled": True,
                  "trigger": {"type": "weekly", "days": ["sat", "sun"], "time": "09:30"},
                  "action": {"audio": "sound-test.mp3", "max_seconds": 120, "devices": {"Kitchen speaker": 0.4}}}
    assert len(store.list_backups()) == 1
    audit = [json.loads(l) for l in store.audit_path().read_text().splitlines()]
    assert audit[-1]["action"] == "create_event" and audit[-1]["actor"] == "dashboard"
    # And the scheduler sees it.
    config, problems = sched.load_config()
    assert not problems and any(e["id"] == "kids-quran" for e in config.events)


@pytest.mark.parametrize("over,needle", [
    ({"days": []}, "non-empty"),
    ({"time": ""}, "bad trigger"),
    ({"audio": ""}, "no audio"),
    ({"dev_2_on": ""}, "non-empty object"),
    ({"id": "Bad ID!"}, "id may only use"),
    ({"id": "prayer-fajr"}, "already exists"),
])
def test_invalid_form_rejected_config_unchanged(client, dash, over, needle):
    store = dash[1]
    before = sched.CONFIG_PATH.read_bytes()
    r = client.post("/events/save", data=form_for_weekly(client.csrf, version(store), **over))
    assert r.status_code == 422
    assert needle in r.text
    assert sched.CONFIG_PATH.read_bytes() == before


def test_stale_version_gets_409(client, dash):
    store = dash[1]
    old = version(store)
    assert client.post("/events/save", data=form_for_weekly(client.csrf, old)).status_code == 303
    # Second tab still has the old version.
    r = client.post("/events/save", data=form_for_weekly(client.csrf, old, eid="other"))
    assert r.status_code == 409
    assert "changed by someone else" in r.text
    assert "other" not in [e["id"] for e in raw_config()["events"]]


def test_edit_preserves_unknown_keys(client, dash):
    store = dash[1]
    raw = raw_config()
    raw["custom_top"] = {"keep": True}
    raw["events"][5]["notes"] = "keep me"
    raw["events"][5]["trigger"]["x_extra"] = 1
    raw["events"][5]["action"]["devices"]["Kitchen speaker"] = {"volume": 0.3, "audio": "sound-test.mp3", "x": 1}
    sched.CONFIG_PATH.write_text(json.dumps(raw, indent=2))
    form = form_for_weekly(client.csrf, version(store), eid="example-weekday-morning",
                           original_id="example-weekday-morning", dev_2_audio="sound-test.mp3", dev_2_vol="50")
    assert client.post("/events/save", data=form).status_code == 303
    new = raw_config()
    assert new["custom_top"] == {"keep": True}
    ev = new["events"][5]
    assert ev["id"] == "example-weekday-morning" and ev["notes"] == "keep me"
    assert ev["trigger"]["x_extra"] == 1
    assert ev["action"]["devices"]["Kitchen speaker"] == {"volume": 0.5, "audio": "sound-test.mp3", "x": 1}
    assert [e["id"] for e in new["events"]] == [e["id"] for e in raw["events"]]   # order kept


def test_prayer_edit_requires_confirm(client, dash):
    store = dash[1]
    form = {"csrf_token": client.csrf, "version": version(store), "original_id": "prayer-fajr",
            "id": "prayer-fajr", "name": "Fajr Adhan", "enabled": "on", "trigger_type": "prayer",
            "prayer": "Fajr", "offset_minutes": "0", "audio": "sound-test.mp3",
            "dev_0_name": "Bedroom display", "dev_0_on": "on", "dev_0_vol": "15"}
    assert client.post("/events/save", data=form).status_code == 422
    form["confirm_prayer"] = "on"
    assert client.post("/events/save", data=form).status_code == 303
    fajr = raw_config()["events"][0]
    assert fajr["action"]["devices"] == {"Bedroom display": 0.15}
    assert "days" not in fajr["trigger"] and "offset_minutes" not in fajr["trigger"]


def test_prayer_delete_requires_confirm(client, dash):
    store = dash[1]
    client.post("/events/prayer-isha/delete", data={"csrf_token": client.csrf, "version": version(store)})
    assert any(e["id"] == "prayer-isha" for e in raw_config()["events"])


def test_delete_and_duplicate(client, dash):
    store = dash[1]
    page = client.get("/events/example-once/duplicate").text
    assert 'value="example-once-copy"' in page
    r = client.post("/events/example-once/delete", data={"csrf_token": client.csrf, "version": version(store)})
    assert r.status_code == 303
    assert "example-once" not in [e["id"] for e in raw_config()["events"]]


def test_toggle(client, dash):
    store = dash[1]
    client.post("/events/example-once/toggle", data={"csrf_token": client.csrf, "version": version(store)})
    assert next(e for e in raw_config()["events"] if e["id"] == "example-once")["enabled"] is True


def test_prayers_matrix(client, dash):
    store = dash[1]
    data = {"csrf_token": client.csrf, "version": version(store)}
    for p in ("fajr", "dhuhr", "asr", "maghrib", "isha"):
        data.update({f"{p}_enabled": "on", f"{p}_audio": "sound-test-alt.mp3",
                     f"{p}_0_on": "on", f"{p}_0_vol": "20", f"{p}_1_on": "on", f"{p}_1_vol": "25"})
    data["isha_enabled"] = ""
    before = raw_config()["events"][0]["action"]["devices"]
    assert client.post("/prayers", data=data).status_code == 303
    assert raw_config()["events"][0]["action"]["devices"] == before   # unchanged: no confirm
    data["confirm_prayer"] = "on"
    data["version"] = version(store)
    assert client.post("/prayers", data=data).status_code == 303
    events = {e["id"]: e for e in raw_config()["events"]}
    assert events["prayer-fajr"]["action"]["devices"] == {"Bedroom display": 0.2, "Living Room display": 0.25}
    assert events["prayer-fajr"]["action"]["audio"] == "sound-test-alt.mp3"
    assert events["prayer-isha"]["enabled"] is False


def test_settings_and_device_mute(client, dash):
    store = dash[1]
    data = {"csrf_token": client.csrf, "version": version(store), "max_volume": "60", "default_volume": "30",
            "latitude": "40.7128", "longitude": "-74.006", "timezone": "America/New_York",
            "calculation_method": "NORTH_AMERICA", "madhab": "HANAFI", "high_latitude_rule": "",
            "grace_seconds": "120", "conflict_window_minutes": "3", "history_days": "90",
            "audio_base_url": "http://192.168.1.50:8080/audio",
            "device_0_name": "Bedroom display", "device_0_enabled": "on",
            "device_1_name": "Living Room display",
            "device_2_name": "Kitchen speaker", "device_2_enabled": "on",
            "new_device": "Kitchen display"}
    assert client.post("/settings", data=data).status_code == 303
    raw = raw_config()
    assert raw["settings"]["max_volume"] == 0.6 and raw["settings"]["madhab"] == "HANAFI"
    assert raw["devices"]["Living Room display"]["enabled"] is False
    assert raw["devices"]["Kitchen display"] == {"enabled": True}
    # Bad value rejected, config untouched.
    before = sched.CONFIG_PATH.read_bytes()
    data.update(version=version(store), max_volume="150", new_device="")
    client.post("/settings", data=data)
    assert sched.CONFIG_PATH.read_bytes() == before


def test_remove_device_in_use_refused(client, dash):
    store = dash[1]
    client.post("/settings/remove-device", data={"csrf_token": client.csrf, "version": version(store),
                                                  "name": "Kitchen speaker"})
    assert "Kitchen speaker" in raw_config()["devices"]


# ------------------------------------------------------------------ save_config internals

def test_backup_rotation_keeps_20(dash):
    store = dash[1]
    for i in range(25):
        raw = raw_config()
        raw["settings"]["history_days"] = 30 + i
        store.save_config(raw, version(store))
    backups = store.list_backups()
    assert len(backups) == 20
    assert raw_config()["settings"]["history_days"] == 54


def test_failed_write_leaves_config_intact(dash, monkeypatch):
    store = dash[1]
    before = sched.CONFIG_PATH.read_bytes()
    raw = raw_config()
    raw["settings"]["history_days"] = 5

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        store.save_config(raw, version(store))
    assert sched.CONFIG_PATH.read_bytes() == before
    json.loads(before)


def test_restore_backup(client, dash):
    store = dash[1]
    original = sched.CONFIG_PATH.read_bytes()
    raw = raw_config()
    raw["settings"]["history_days"] = 7
    store.save_config(raw, version(store))
    name = store.list_backups()[0]["name"]
    r = client.post("/backups/restore", data={"csrf_token": client.csrf, "version": version(store), "name": name})
    assert r.status_code == 303
    assert json.loads(sched.CONFIG_PATH.read_bytes()) == json.loads(original)
    assert len(store.list_backups()) == 2   # restore backed up the current config first
    r = client.post("/backups/restore", data={"csrf_token": client.csrf, "version": version(store),
                                              "name": "../config.json"})
    assert json.loads(sched.CONFIG_PATH.read_bytes()) == json.loads(original)


# ------------------------------------------------------------------ skip / test / play / stop

def _commands():
    return [json.loads(p.read_text()) for p in sorted(sched.COMMANDS_DIR.glob("*.json"))]


def test_skip_and_unskip(client):
    client.post("/events/prayer-asr/skip", data={"csrf_token": client.csrf})
    assert sched.pending_skips() == {"prayer-asr"}
    assert "next one will be skipped" in client.get("/events").text
    client.post("/events/prayer-asr/unskip", data={"csrf_token": client.csrf})
    assert sched.pending_skips() == set()


def test_test_now_queues_play_event(client):
    r = client.post("/events/example-birthday/test", data={"csrf_token": client.csrf})
    assert r.status_code == 303 and r.headers["location"].startswith("/play?cmd=")
    cmds = _commands()
    assert len(cmds) == 1 and cmds[0]["action"] == "play_event"
    assert cmds[0]["event_id"] == "example-birthday"


def test_play_now_caps_volume(client, dash):
    data = {"csrf_token": client.csrf, "audio": "sound-test.mp3", "max_seconds": "30",
            "dev_0_name": "Kitchen speaker", "dev_0_on": "on", "dev_0_vol": "100"}
    assert client.post("/play", data=data).status_code == 303
    cmd = _commands()[0]
    assert cmd["action"] == "play_now" and cmd["devices"] == {"Kitchen speaker": 0.8}
    assert cmd["max_seconds"] == 30


@pytest.mark.parametrize("body,needle", [
    ({"audio": "../app/config.json", "devices": {"Kitchen speaker": 0.2}}, "choose an audio"),
    ({"audio": "sound-test-missing.mp3", "devices": {"Kitchen speaker": 0.2}}, "not found"),
    ({"audio": "sound-test.mp3", "devices": {"Nope": 0.2}}, "unknown device"),
    ({"audio": "sound-test.mp3", "devices": {"Kitchen speaker": 5}}, "between 0 and 1"),
    ({"audio": "sound-test.mp3", "devices": {}}, "at least one device"),
    ({"audio": "sound-test.mp3", "devices": {"Kitchen speaker": 0.2}, "max_seconds": -1}, "max seconds"),
])
def test_api_play_validation(api, body, needle):
    r = api.post("/api/play", json=body)
    assert r.status_code == 422 and needle in json.dumps(r.json())
    assert not sched.COMMANDS_DIR.exists() or _commands() == []


def test_api_play_and_command_result(api, monkeypatch):
    r = api.post("/api/play", json={"audio": "sound-test.mp3", "devices": {"Kitchen speaker": 0.95}})
    assert r.status_code == 202
    cmd = r.json()
    assert cmd["devices"] == {"Kitchen speaker": 0.8} and cmd["actor"] == "api"
    assert api.get(f"/api/commands/{cmd['id']}").json()["pending"] is True
    # The scheduler picks it up.
    config, _ = sched.load_config()
    monkeypatch.setattr(sched, "execute_command", lambda c, cfg: {"status": "started"})
    sched.process_commands(config, datetime.now(TZ))
    res = api.get(f"/api/commands/{cmd['id']}").json()
    assert res["pending"] is False and res["result"]["status"] == "started"
    assert api.get("/api/commands/../../etc").status_code == 404


def test_stop(client, api):
    client.post("/stop", data={"csrf_token": client.csrf, "device": "Kitchen speaker"})
    assert _commands()[-1]["devices"] == ["Kitchen speaker"]
    r = api.post("/api/stop")
    assert r.status_code == 202 and len(r.json()["devices"]) == 3
    assert api.post("/api/stop", json={"devices": ["Ghost"]}).status_code == 422


# ------------------------------------------------------------------ audio library

def _upload(client, name, content, **extra):
    return client.post("/library/upload", data={"csrf_token": client.csrf, **extra},
                       files={"file": (name, io.BytesIO(content), "audio/mpeg")})


def test_upload_good_file(client):
    assert _upload(client, "Sound Test (2).mp3", MP3).status_code == 303
    assert (sched.AUDIO_DIR / "Sound-Test-2.mp3").read_bytes() == MP3


@pytest.mark.parametrize("name,content,needle", [
    ("evil.exe", MP3, "must be one of"),
    ("sound-test-script.mp3.sh", MP3, "must be one of"),
    ("sound-test-fake.mp3", b"<?php echo 1; ?>" * 10, "does not look like"),
    ("fake.wav", MP3, "does not look like"),
    ("sound-test-empty.mp3", b"", "empty"),
    ("....mp3", MP3, "usable name"),
])
def test_upload_rejected(client, name, content, needle):
    before = sorted(p.name for p in sched.AUDIO_DIR.iterdir())
    r = _upload(client, name, content)
    assert r.status_code == 303
    assert needle in client.get("/library").text
    assert sorted(p.name for p in sched.AUDIO_DIR.iterdir()) == before


def test_upload_traversal_name_stays_in_library(client):
    _upload(client, "../../app/sound-test-evil.mp3", MP3)
    assert (sched.AUDIO_DIR / "sound-test-evil.mp3").exists()
    assert not (sched.AUDIO_DIR.parent / "app" / "sound-test-evil.mp3").exists()


def test_upload_too_large(client, dash, monkeypatch):
    monkeypatch.setattr(dash[1], "UPLOAD_MAX_BYTES", 100)
    _upload(client, "sound-test-big.mp3", MP3)
    assert not (sched.AUDIO_DIR / "sound-test-big.mp3").exists()
    assert not list(sched.AUDIO_DIR.glob("*.tmp")) and not list(sched.AUDIO_DIR.glob(".upload-*"))


def test_upload_overwrite_needs_flag(client):
    _upload(client, "sound-test.mp3", MP3 + b"new")
    assert (sched.AUDIO_DIR / "sound-test.mp3").read_bytes() != MP3 + b"new"
    _upload(client, "sound-test.mp3", MP3 + b"new", overwrite="on")
    assert (sched.AUDIO_DIR / "sound-test.mp3").read_bytes() == MP3 + b"new"


def test_other_audio_types_accepted(client):
    assert _upload(client, "x.m4a", b"\x00\x00\x00\x20ftypM4A " + b"\0" * 50).status_code == 303
    assert _upload(client, "x.wav", b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\0" * 50).status_code == 303
    assert _upload(client, "x.ogg", b"OggS" + b"\0" * 50).status_code == 303
    assert {"x.m4a", "x.wav", "x.ogg"} <= {p.name for p in sched.AUDIO_DIR.iterdir()}


def test_delete_referenced_audio_refused(client, api):
    client.post("/library/delete", data={"csrf_token": client.csrf, "name": "sound-test.mp3"})
    assert (sched.AUDIO_DIR / "sound-test.mp3").exists()
    assert "prayer-fajr" in client.get("/library").text
    r = api.delete("/api/audio/sound-test.mp3")
    assert r.status_code == 422 and "prayer-fajr" in r.text
    # Unreferenced file can go; traversal can't.
    assert api.delete("/api/audio/sound-test-alt.mp3").status_code == 200
    assert not (sched.AUDIO_DIR / "sound-test-alt.mp3").exists()
    assert api.delete("/api/audio/..%2Fapp%2Fconfig.json").status_code in (404, 422)
    assert sched.CONFIG_PATH.exists()


# ------------------------------------------------------------------ read-only API

def test_api_events_crud(api, dash):
    store = dash[1]
    ev = {"id": "api-event", "enabled": True, "trigger": {"type": "once", "datetime": "2030-01-01T10:00"},
          "action": {"audio": "sound-test.mp3", "devices": {"Kitchen speaker": 0.3}}}
    r = api.post("/api/events", json={"event": ev, "version": version(store)})
    assert r.status_code == 201
    got = api.get("/api/events/api-event").json()
    assert got["event"] == ev
    ev["action"]["devices"]["Kitchen speaker"] = 0.1
    r = api.put("/api/events/api-event", json={"event": ev}, headers={"If-Match": "stale"})
    assert r.status_code == 409
    r = api.put("/api/events/api-event", json={"event": ev}, headers={"If-Match": got["version"]})
    assert r.status_code == 200
    assert api.delete("/api/events/api-event").status_code == 200
    assert api.get("/api/events/api-event").status_code == 404
    # Prayer events need confirm.
    fajr = api.get("/api/events/prayer-fajr").json()["event"]
    assert api.put("/api/events/prayer-fajr", json={"event": fajr}).status_code == 422
    assert api.put("/api/events/prayer-fajr", json={"event": fajr, "confirm": True}).status_code == 200
    assert api.delete("/api/events/prayer-fajr").status_code == 422
    # Bad event.
    bad = dict(ev, id="bad", trigger={"type": "weekly", "days": ["funday"], "time": "07:00"})
    r = api.post("/api/events", json={"event": bad})
    assert r.status_code == 422 and "unknown day" in json.dumps(r.json())
    actors = {json.loads(l)["actor"] for l in store.audit_path().read_text().splitlines()}
    assert actors == {"api"}


def test_api_schedule_and_status(api, dash):
    r = api.get("/api/schedule?date=2026-11-18").json()
    assert [o["event"] for o in r["occurrences"]] == ["prayer-fajr", "prayer-dhuhr", "prayer-asr",
                                                      "prayer-maghrib", "prayer-isha"]
    assert r["prayers"]["Fajr"].startswith("2026-11-18T05:30")
    assert api.get("/api/schedule?date=nope").status_code == 422

    st = api.get("/api/status").json()
    assert st["alive"] is False and len(st["next_computed"]) == 5
    sched.write_status(sched.load_config()[0], datetime.now(TZ), sched.State(), None, None)
    assert api.get("/api/status").json()["alive"] is True
    old = json.loads(sched.STATUS_PATH.read_text())
    old["updated"] = (datetime.now(TZ) - timedelta(seconds=120)).isoformat()
    sched.STATUS_PATH.write_text(json.dumps(old))
    assert api.get("/api/status").json()["alive"] is False


def test_api_history_filter(api):
    sched.record_history("prayer-fajr", "Kitchen speaker", "sound-test.mp3", 0.2, "playing")
    sched.record_history("prayer-fajr", "Living Room display", "sound-test.mp3", 0.2, "not_found")
    rows = api.get("/api/history?status=not_found").json()["history"]
    assert [r["device"] for r in rows] == ["Living Room display"]
    assert len(api.get("/api/history?device=Kitchen%20speaker").json()["history"]) == 1


def test_api_settings_and_devices(api, dash):
    store = dash[1]
    r = api.put("/api/settings", json={"settings": {"max_volume": 2}})
    assert r.status_code == 422
    r = api.put("/api/settings", json={"settings": {"max_volume": 0.5}, "version": version(store)})
    assert r.status_code == 200 and raw_config()["settings"]["max_volume"] == 0.5
    names = [d["name"] for d in api.get("/api/devices").json()["devices"]]
    assert names == ["Bedroom display", "Living Room display", "Kitchen speaker"]


def test_api_upload(api):
    r = api.post("/api/audio", files={"file": ("sound-test-upload.mp3", io.BytesIO(MP3), "audio/mpeg")})
    assert r.status_code == 201 and r.json()["name"] == "sound-test-upload.mp3"
    names = [a["name"] for a in api.get("/api/audio").json()["audio"]]
    assert "sound-test-upload.mp3" in names


# ------------------------------------------------------------------ helpers

def test_describe_trigger(dash):
    store = dash[1]
    assert store.describe_trigger({"type": "prayer", "prayer": "Fajr", "offset_minutes": -45}) == "45 min before Fajr"
    assert store.describe_trigger({"type": "prayer", "prayer": "Dhuhr", "days": ["fri"]}) == "Dhuhr (Fri)"
    assert store.describe_trigger({"type": "weekly", "days": ["mon", "tue", "wed", "thu", "fri"], "time": "07:00"}) == "Weekdays at 07:00"
    assert store.describe_trigger({"type": "yearly", "month": 6, "day": 15, "time": "08:00"}) == "Every Jun 15 at 08:00"
    assert store.describe_trigger({"type": "once", "datetime": "2026-12-31T20:00"}) == "Once: Dec 31, 2026 at 20:00"


def test_next_occurrence(dash):
    store = dash[1]
    config, _ = sched.load_config()
    now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
    bday = next(e for e in config.events if e["id"] == "example-birthday")
    assert store.next_occurrence(dict(bday, enabled=True), config, now) == datetime(2027, 6, 15, 8, 0, tzinfo=TZ)
    assert store.next_occurrence(bday, config, now) is None   # disabled
    fajr = config.events[0]
    assert store.next_occurrence(fajr, config, now) == datetime(2026, 11, 19, 5, 30, tzinfo=TZ)


def test_form_roundtrip_all_types(dash):
    ad = dash[0]

    class F(dict):
        def getlist(self, k):
            v = self.get(k, [])
            return v if isinstance(v, list) else [v]
    base = {"id": "x", "enabled": "on", "audio": "sound-test.mp3",
            "dev_0_name": "Kitchen speaker", "dev_0_on": "on", "dev_0_vol": "25"}
    cases = [
        ({"trigger_type": "prayer", "prayer": "Fajr", "offset_minutes": "-45", "days": ["fri"]},
         {"type": "prayer", "prayer": "Fajr", "offset_minutes": -45, "days": ["fri"]}),
        ({"trigger_type": "prayer", "prayer": "Asr", "offset_minutes": "0", "days": sched.WEEKDAYS},
         {"type": "prayer", "prayer": "Asr"}),
        ({"trigger_type": "yearly", "month": "2", "day": "29", "time": "09:00"},
         {"type": "yearly", "month": 2, "day": 29, "time": "09:00"}),
        ({"trigger_type": "once", "once_datetime": "2027-01-01T10:00"},
         {"type": "once", "datetime": "2027-01-01T10:00"}),
    ]
    for fields, trigger in cases:
        ev = ad.event_from_form(F(base, **fields))
        assert ev["trigger"] == trigger
        assert sched.validate_event(ev, set()) == []
