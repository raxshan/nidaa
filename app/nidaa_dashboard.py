"""Nidaa dashboard: LAN web UI + REST API for the Nidaa scheduler (plan Phase 2).

Run (single worker; file-based coordination assumes it):
    uvicorn nidaa_dashboard:app --host 0.0.0.0 --port 8081 --workers 1

Environment:
    NIDAA_DASHBOARD_PASSWORD   shared login password (required; no login without it)
    NIDAA_SESSION_SECRET       cookie signing key (random per start if unset)
    NIDAA_API_TOKEN            bearer token for /api/* scripts and MCP (API token auth off if unset)

The dashboard only edits config/skips and queues commands. The scheduler does all playback.
"""

import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import nidaa_scheduler as sched
import nidaa_store as store
from nidaa_store import ConfigError, NotFound, VersionConflict

BASE_DIR = Path(__file__).resolve().parent
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
SESSION_MAX_AGE = 30 * 24 * 3600


# ============================================================
# AUTH HELPERS
# ============================================================

class LoginLimiter:
    """5 failures from one client -> locked out for 60 s."""

    def __init__(self, max_failures=LOGIN_MAX_FAILURES, lockout=LOGIN_LOCKOUT_SECONDS):
        self.max_failures, self.lockout = max_failures, lockout
        self._failures, self._locked_until = {}, {}
        self._lock = threading.Lock()

    def locked_for(self, key):
        with self._lock:
            remaining = self._locked_until.get(key, 0) - time.monotonic()
            return max(0, int(remaining + 0.999))

    def fail(self, key):
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.max_failures:
                self._locked_until[key] = time.monotonic() + self.lockout
                self._failures[key] = 0

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


class LoginRequired(Exception):
    pass


def _client_key(request):
    return request.client.host if request.client else "unknown"


def _is_logged_in(request):
    return request.session.get("auth") is True


def require_ui(request):
    if not _is_logged_in(request):
        raise LoginRequired()


async def require_ui_post(request):
    """Logged-in + CSRF token from the form (or X-CSRF-Token header). Returns the form."""
    require_ui(request)
    form = await request.form()
    token = form.get("csrf_token") or request.headers.get("x-csrf-token") or ""
    if not hmac.compare_digest(str(token), str(request.session.get("csrf", ""))):
        raise HTTPException(403, "CSRF check failed. Reload the page and try again.")
    return form


def api_actor(request):
    """Bearer token (scripts/MCP) or the dashboard's own session + CSRF header."""
    header = request.headers.get("authorization", "")
    token = request.app.state.api_token
    if header.lower().startswith("bearer "):
        if token and hmac.compare_digest(header[7:].strip().encode(), token.encode()):
            return "api"
        raise HTTPException(401, "invalid API token")
    if _is_logged_in(request):
        if request.method in ("GET", "HEAD"):
            return "dashboard"
        sent = request.headers.get("x-csrf-token", "")
        if hmac.compare_digest(sent, str(request.session.get("csrf", ""))):
            return "dashboard"
        raise HTTPException(403, "CSRF check failed")
    raise HTTPException(401, "authentication required")


def flash(request, message, kind="ok"):
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def _errors_text(exc):
    return "; ".join(exc.problems) if isinstance(exc, ConfigError) else str(exc)


# ============================================================
# FORM -> EVENT
# ============================================================

def _num(value, kind=float, default=None):
    if value is None or str(value).strip() == "":
        return default
    try:
        return kind(str(value).strip())
    except ValueError:
        raise ConfigError([f"'{value}' is not a valid number"])


def device_rows(form):
    """Collect dev_<i>_* fields -> {name: (on, volume 0-1, audio override)}."""
    rows = {}
    i = 0
    while f"dev_{i}_name" in form:
        name = form[f"dev_{i}_name"]
        vol = _num(form.get(f"dev_{i}_vol"), float, 0) / 100
        rows[name] = (form.get(f"dev_{i}_on") == "on", round(vol, 2), (form.get(f"dev_{i}_audio") or "").strip())
        i += 1
    return rows


def event_from_form(form, base=None):
    """Build an event dict from the event form, keeping unknown keys of `base`."""
    ev = dict(base or {})
    ev["id"] = (form.get("id") or "").strip()
    name = (form.get("name") or "").strip()
    if name:
        ev["name"] = name
    else:
        ev.pop("name", None)
    ev["enabled"] = form.get("enabled") == "on"

    priority = _num(form.get("priority"), int)
    if priority is None:
        ev.pop("priority", None)
    else:
        ev["priority"] = priority

    for field in ("active_from", "active_until"):
        value = (form.get(field) or "").strip()
        if value:
            ev[field] = value
        else:
            ev.pop(field, None)

    ttype = form.get("trigger_type")
    old_trigger = (base or {}).get("trigger", {})
    known = {"type", "prayer", "offset_minutes", "days", "time", "month", "day", "datetime"}
    trigger = {k: v for k, v in old_trigger.items() if k not in known}
    trigger["type"] = ttype
    if ttype == "prayer":
        trigger["prayer"] = form.get("prayer")
        offset = _num(form.get("offset_minutes"), int, 0)
        if offset:
            trigger["offset_minutes"] = offset
        days = form.getlist("days")
        if days and set(days) != set(sched.WEEKDAYS):
            trigger["days"] = [d for d in sched.WEEKDAYS if d in days]
    elif ttype == "weekly":
        trigger["days"] = [d for d in sched.WEEKDAYS if d in form.getlist("days")]
        trigger["time"] = form.get("time") or ""
    elif ttype == "yearly":
        trigger["month"] = _num(form.get("month"), int)
        trigger["day"] = _num(form.get("day"), int)
        trigger["time"] = form.get("time") or ""
    elif ttype == "once":
        trigger["datetime"] = (form.get("once_datetime") or "").strip()
    ev["trigger"] = trigger

    old_action = (base or {}).get("action", {})
    action = {k: v for k, v in old_action.items() if k not in ("audio", "devices", "max_seconds")}
    audio = (form.get("audio") or "").strip()
    if audio:
        action["audio"] = audio
    max_seconds = _num(form.get("max_seconds"), float)
    if max_seconds:
        action["max_seconds"] = int(max_seconds) if max_seconds == int(max_seconds) else max_seconds
    old_devices = old_action.get("devices", {}) if isinstance(old_action.get("devices"), dict) else {}
    devices = {}
    for dname, (on, vol, override) in device_rows(form).items():
        if not on:
            continue
        old = old_devices.get(dname)
        if override:
            spec = dict(old) if isinstance(old, dict) else {}
            spec.update({"audio": override, "volume": vol})
            devices[dname] = spec
        else:
            devices[dname] = vol
    action["devices"] = devices
    ev["action"] = action
    return ev


# ============================================================
# VIEW HELPERS
# ============================================================

def device_specs(event, config):
    """[(name, on, volume 0-100, audio override)] for every configured device."""
    devices = (event.get("action") or {}).get("devices", {}) if event else {}
    rows = []
    for name in config.devices:
        spec = devices.get(name)
        if spec is None:
            rows.append((name, False, int(config.settings["default_volume"] * 100), ""))
        elif sched._is_number(spec):
            rows.append((name, True, round(spec * 100), ""))
        else:
            rows.append((name, True, round(spec.get("volume", config.settings["default_volume"]) * 100),
                         spec.get("audio", "")))
    return rows


def event_rows(raw, config, now):
    skips = sched.pending_skips()
    valid = {e["id"]: e for e in config.events}
    rows = []
    for ev in raw.get("events", []):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id")
        parsed_ev = valid.get(eid)
        nxt = store.next_occurrence(parsed_ev, config, now) if parsed_ev else None
        rows.append({
            "id": eid, "name": ev.get("name") or eid, "type": (ev.get("trigger") or {}).get("type"),
            "schedule": store.describe_trigger(ev.get("trigger") or {}),
            "enabled": ev.get("enabled", True), "valid": parsed_ev is not None,
            "next": nxt, "skip_pending": eid in skips, "prayer": store.is_prayer_event(ev),
            "devices": list(((ev.get("action") or {}).get("devices") or {}).keys()),
        })
    return rows


def fmt_when(value, now=None):
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    now = now or datetime.now(value.tzinfo)
    if value.date() == now.date():
        day = "Today"
    elif value.date() == now.date() + timedelta(days=1):
        day = "Tomorrow"
    elif value.year == now.year:
        day = value.strftime("%a %b %-d")
    else:
        day = value.strftime("%b %-d, %Y")
    return f"{day} {value.strftime('%-I:%M %p')}"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(sec):
    if sec is None:
        return "—"
    return f"{sec // 60}:{sec % 60:02d}"


# ============================================================
# APP
# ============================================================

def create_app():
    app = FastAPI(title="Nidaa dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.password = os.environ.get("NIDAA_DASHBOARD_PASSWORD") or None
    app.state.api_token = os.environ.get("NIDAA_API_TOKEN") or None
    app.state.limiter = LoginLimiter()
    secret = os.environ.get("NIDAA_SESSION_SECRET") or secrets.token_urlsafe(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="nidaa_session",
                       max_age=SESSION_MAX_AGE, same_site="strict", https_only=False)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters.update(when=fmt_when, size=fmt_size, duration=fmt_duration,
                                 pct=lambda v: f"{round(v * 100)}%")

    def render(request, name, **ctx):
        raw, version = store.read_config()
        ctx.setdefault("settings", raw.get("settings", {}))
        flashes = request.session.pop("flash", [])
        return templates.TemplateResponse(request, name, {
            "csrf": request.session.get("csrf", ""), "flashes": flashes, "version": version,
            "nav": name.split(".")[0], "audio_base": raw.get("settings", {}).get(
                "audio_base_url", sched.DEFAULT_SETTINGS["audio_base_url"]).rstrip("/"),
            "quote": quote, **ctx})

    def back(url, request=None, message=None, kind="ok"):
        if request is not None and message:
            flash(request, message, kind)
        return RedirectResponse(url, status_code=303)

    def load():
        raw, version = store.read_config()
        return raw, version, store.parsed(raw)

    @app.exception_handler(LoginRequired)
    async def _login_required(request, exc):
        nxt = request.url.path if request.method == "GET" else "/"
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)

    @app.exception_handler(ConfigError)
    async def _config_error(request, exc):
        return JSONResponse({"errors": exc.problems}, status_code=422)

    @app.exception_handler(VersionConflict)
    async def _conflict(request, exc):
        return JSONResponse({"error": str(exc), "version": store.current_version()}, status_code=409)

    @app.exception_handler(NotFound)
    async def _not_found(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=404)

    # -------------------------------------------------------------- login

    @app.get("/login")
    def login_page(request: Request, next: str = "/"):
        return templates.TemplateResponse(request, "login.html", {
            "next": next if next.startswith("/") and not next.startswith("//") else "/",
            "error": None, "configured": app.state.password is not None, "nav": "login"})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        nxt = form.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        key = _client_key(request)
        ctx = {"next": nxt, "configured": app.state.password is not None, "nav": "login"}
        wait = app.state.limiter.locked_for(key)
        if wait:
            return templates.TemplateResponse(request, "login.html", {
                **ctx, "error": f"Too many attempts. Try again in {wait} s."}, status_code=429)
        password = str(form.get("password") or "")
        if app.state.password and hmac.compare_digest(password.encode(), app.state.password.encode()):
            app.state.limiter.reset(key)
            request.session.clear()
            request.session.update({"auth": True, "csrf": secrets.token_urlsafe(24)})
            return RedirectResponse(nxt, status_code=303)
        app.state.limiter.fail(key)
        return templates.TemplateResponse(request, "login.html", {
            **ctx, "error": "Wrong password."}, status_code=401)

    @app.post("/logout")
    async def logout(request: Request):
        await require_ui_post(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # -------------------------------------------------------------- today

    @app.get("/")
    def today(request: Request):
        require_ui(request)
        raw, version, config = load()
        now = datetime.now(config.tz)
        prayers = store.today_prayer_times(config, now.date())
        schedule = store.schedule_for_day(config, now.date(), store.read_state())
        status = store.read_status()
        nxt = sched.upcoming(config, now, sched.State(), limit=1)
        return render(request, "today.html", now=now, prayers=prayers, schedule=schedule,
                      status=status, next_occ=nxt[0] if nxt else None, devices=raw.get("devices", {}))

    # -------------------------------------------------------------- events

    @app.get("/events")
    def events(request: Request):
        require_ui(request)
        raw, version, config = load()
        now = datetime.now(config.tz)
        return render(request, "events.html", rows=event_rows(raw, config, now), now=now)

    def _form_page(request, event, mode, original_id=None, errors=None):
        raw, version, config = load()
        return render(request, "event_form.html", event=event, mode=mode, original_id=original_id,
                      errors=errors or [], devices=device_specs(event, config),
                      audio_files=store.audio_names(), prayers=sched.PRAYER_NAMES,
                      weekdays=sched.WEEKDAYS, months=store.MONTHS,
                      max_volume=int(config.settings["max_volume"] * 100),
                      is_prayer=store.is_prayer_event(event or {}))

    @app.get("/events/new")
    def new_event(request: Request, type: str = "weekly"):
        require_ui(request)
        if type not in ("prayer", "weekly", "yearly", "once"):
            type = "weekly"
        return _form_page(request, {"enabled": True, "trigger": {"type": type}, "action": {}}, "new")

    @app.get("/events/{event_id}/edit")
    def edit_event(request: Request, event_id: str):
        require_ui(request)
        raw, _ = store.read_config()
        _, ev = store.find_event(raw, event_id)
        return _form_page(request, ev, "edit", original_id=event_id)

    @app.get("/events/{event_id}/duplicate")
    def duplicate_event(request: Request, event_id: str):
        require_ui(request)
        raw, _ = store.read_config()
        _, ev = store.find_event(raw, event_id)
        copy_ev = json.loads(json.dumps(ev))
        copy_ev["id"] = store.unique_copy_id(raw, event_id)
        copy_ev["name"] = f"{ev.get('name') or event_id} (copy)"
        copy_ev["enabled"] = False
        return _form_page(request, copy_ev, "new")

    @app.post("/events/save")
    async def save_event(request: Request):
        form = await require_ui_post(request)
        original_id = form.get("original_id") or None
        base = ev = None
        try:
            if original_id:
                raw, _ = store.read_config()
                _, base = store.find_event(raw, original_id)
            ev = event_from_form(form, base)
            if original_id:
                store.update_event(original_id, ev, form.get("version"),
                                   confirm_prayer=form.get("confirm_prayer") == "on")
            else:
                store.create_event(ev, form.get("version"))
        except (ConfigError, VersionConflict) as exc:
            event = ev or base or {"trigger": {"type": form.get("trigger_type")}}
            problems = exc.problems if isinstance(exc, ConfigError) else [str(exc)]
            resp = _form_page(request, event, "edit" if original_id else "new", original_id, problems)
            resp.status_code = 409 if isinstance(exc, VersionConflict) else 422
            return resp
        return back("/events", request, f"Saved {ev['id']}.")

    @app.post("/events/{event_id}/toggle")
    async def toggle_event(request: Request, event_id: str):
        form = await require_ui_post(request)
        raw, _ = store.read_config()
        _, ev = store.find_event(raw, event_id)
        ev = dict(ev, enabled=not ev.get("enabled", True))
        try:
            store.update_event(event_id, ev, form.get("version"),
                               confirm_prayer=form.get("confirm_prayer") == "on")
        except (ConfigError, VersionConflict) as exc:
            return back("/events", request, _errors_text(exc), "error")
        return back("/events", request, f"{event_id} {'enabled' if ev['enabled'] else 'disabled'}.")

    @app.post("/events/{event_id}/skip")
    async def skip_event(request: Request, event_id: str):
        await require_ui_post(request)
        raw, _ = store.read_config()
        store.find_event(raw, event_id)
        sched.add_skip(event_id)
        store.audit("dashboard", "skip_next", {"id": event_id})
        return back(request.headers.get("referer") or "/events", request,
                    f"The next {event_id} will be skipped.")

    @app.post("/events/{event_id}/unskip")
    async def unskip_event(request: Request, event_id: str):
        await require_ui_post(request)
        sched.remove_skip(event_id)
        store.audit("dashboard", "cancel_skip", {"id": event_id})
        return back(request.headers.get("referer") or "/events", request, f"Skip for {event_id} cancelled.")

    @app.post("/events/{event_id}/test")
    async def test_event(request: Request, event_id: str):
        await require_ui_post(request)
        raw, _ = store.read_config()
        store.find_event(raw, event_id)
        cmd = store.queue_command("play_event", event_id=event_id)
        return back(f"/play?cmd={cmd['id']}", request, f"Test of {event_id} queued.")

    @app.post("/events/{event_id}/delete")
    async def delete_event_ui(request: Request, event_id: str):
        form = await require_ui_post(request)
        try:
            store.delete_event(event_id, form.get("version"),
                               confirm_prayer=form.get("confirm_prayer") == "on")
        except (ConfigError, VersionConflict) as exc:
            return back("/events", request, _errors_text(exc), "error")
        return back("/events", request, f"Deleted {event_id}.")

    # -------------------------------------------------------------- prayers matrix

    def prayer_matrix(raw, config):
        rows = []
        for prayer in sched.PRAYER_NAMES:
            ev = next((e for e in raw.get("events", []) if isinstance(e, dict)
                       and (e.get("trigger") or {}).get("type") == "prayer"
                       and e["trigger"].get("prayer") == prayer
                       and not e["trigger"].get("offset_minutes")), None)
            rows.append({"prayer": prayer, "event": ev,
                         "devices": device_specs(ev, config) if ev else []})
        return rows

    @app.get("/prayers")
    def prayers(request: Request):
        require_ui(request)
        raw, version, config = load()
        return render(request, "prayers.html", rows=prayer_matrix(raw, config),
                      device_names=list(config.devices), audio_files=store.audio_names(),
                      max_volume=int(config.settings["max_volume"] * 100))

    @app.post("/prayers")
    async def save_prayers(request: Request):
        form = await require_ui_post(request)
        if form.get("confirm_prayer") != "on":
            return back("/prayers", request, "Tick the confirm box to change prayer events.", "error")
        raw, _ = store.read_config()
        config = store.parsed(raw)
        names = list(config.devices)
        for prayer in sched.PRAYER_NAMES:
            ev = next((e for e in raw.get("events", []) if isinstance(e, dict)
                       and (e.get("trigger") or {}).get("type") == "prayer"
                       and e["trigger"].get("prayer") == prayer
                       and not e["trigger"].get("offset_minutes")), None)
            if ev is None:
                continue
            p = prayer.lower()
            ev["enabled"] = form.get(f"{p}_enabled") == "on"
            action = ev.setdefault("action", {})
            audio = (form.get(f"{p}_audio") or "").strip()
            if audio:
                action["audio"] = audio
            old = action.get("devices", {})
            devices = {}
            for i, name in enumerate(names):
                if form.get(f"{p}_{i}_on") != "on":
                    continue
                vol = round(_num(form.get(f"{p}_{i}_vol"), float, 0) / 100, 2)
                prev = old.get(name)
                devices[name] = dict(prev, volume=vol) if isinstance(prev, dict) else vol
            action["devices"] = devices
        try:
            store.save_config(raw, form.get("version"), action="update_prayers")
        except (ConfigError, VersionConflict) as exc:
            return back("/prayers", request, _errors_text(exc), "error")
        return back("/prayers", request, "Prayer settings saved. They apply from the next prayer.")

    # -------------------------------------------------------------- history

    @app.get("/history")
    def history(request: Request, device: str = "", status: str = "", event: str = "", limit: int = 200):
        require_ui(request)
        raw, version, config = load()
        rows = store.read_history(limit=max(1, min(limit, 2000)), device=device or None,
                                  status=status or None, event=event or None)
        all_rows = store.read_history(limit=5000)
        return render(request, "history.html", rows=rows, device=device, status=status, event=event,
                      device_names=sorted({r.get("device") for r in all_rows if r.get("device")} | set(config.devices)),
                      statuses=sorted({r.get("status") for r in all_rows if r.get("status")}))

    # -------------------------------------------------------------- library

    @app.get("/library")
    def library(request: Request):
        require_ui(request)
        raw, version, config = load()
        return render(request, "library.html", files=store.list_audio(config),
                      max_mb=store.UPLOAD_MAX_BYTES // (1024 * 1024))

    @app.post("/library/upload")
    async def upload(request: Request):
        form = await require_ui_post(request)
        upload_file = form.get("file")
        if upload_file is None or not getattr(upload_file, "filename", ""):
            return back("/library", request, "Choose a file to upload.", "error")
        try:
            name = store.save_upload(upload_file.filename, upload_file.file,
                                     overwrite=form.get("overwrite") == "on")
        except ConfigError as exc:
            return back("/library", request, _errors_text(exc), "error")
        return back("/library", request, f"Uploaded {name}.")

    @app.post("/library/delete")
    async def delete_audio(request: Request):
        form = await require_ui_post(request)
        raw, version, config = load()
        try:
            store.delete_audio(form.get("name") or "", config)
        except (ConfigError, NotFound) as exc:
            return back("/library", request, _errors_text(exc), "error")
        return back("/library", request, f"Deleted {form.get('name')}.")

    # -------------------------------------------------------------- manual play

    @app.get("/play")
    def play_page(request: Request, cmd: str = ""):
        require_ui(request)
        raw, version, config = load()
        return render(request, "play.html", audio_files=store.audio_names(),
                      devices=[(n, int(config.settings["default_volume"] * 100), spec.get("enabled", True))
                               for n, spec in config.devices.items()],
                      max_volume=int(config.settings["max_volume"] * 100), cmd=cmd,
                      status=store.read_status())

    @app.post("/play")
    async def play_now(request: Request):
        form = await require_ui_post(request)
        raw, version, config = load()
        try:
            devices = {name: vol for name, (on, vol, _) in device_rows(form).items() if on}
            cmd = store.queue_play_now(form.get("audio") or "", devices,
                                       _num(form.get("max_seconds"), float), config)
        except ConfigError as exc:
            return back("/play", request, _errors_text(exc), "error")
        return back(f"/play?cmd={cmd['id']}", request, "Play request sent to the scheduler.")

    @app.post("/stop")
    async def stop(request: Request):
        form = await require_ui_post(request)
        raw, version, config = load()
        device = form.get("device")
        try:
            cmd = store.queue_stop([device] if device else None, config)
        except ConfigError as exc:
            return back("/play", request, _errors_text(exc), "error")
        return back(f"/play?cmd={cmd['id']}", request, f"Stop sent to {device or 'all devices'}.")

    # -------------------------------------------------------------- settings

    @app.get("/settings")
    def settings_page(request: Request):
        require_ui(request)
        raw, version, config = load()
        return render(request, "settings.html", s=config.settings, devices=raw.get("devices", {}),
                      methods=store.calculation_methods(), backups=store.list_backups()[:5])

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await require_ui_post(request)
        raw, _ = store.read_config()
        s = raw.setdefault("settings", {})
        try:
            for key, kind in (("latitude", float), ("longitude", float), ("grace_seconds", int),
                              ("conflict_window_minutes", int), ("history_days", int)):
                if form.get(key) not in (None, ""):
                    s[key] = _num(form.get(key), kind)
            for key in ("max_volume", "default_volume"):
                if form.get(key) not in (None, ""):
                    s[key] = round(_num(form.get(key), float) / 100, 2)
            for key in ("timezone", "calculation_method", "audio_base_url"):
                if form.get(key):
                    s[key] = form.get(key).strip()
            for key in ("madhab", "high_latitude_rule"):
                value = (form.get(key) or "").strip()
                if value:
                    s[key] = value
                else:
                    s.pop(key, None)
            for i, name in enumerate(list(raw.get("devices", {}))):
                if f"device_{i}_name" in form and form[f"device_{i}_name"] == name:
                    raw["devices"][name]["enabled"] = form.get(f"device_{i}_enabled") == "on"
            new_device = (form.get("new_device") or "").strip()
            if new_device:
                if new_device in raw.setdefault("devices", {}):
                    raise ConfigError([f"device '{new_device}' already exists"])
                raw["devices"][new_device] = {"enabled": True}
            store.save_config(raw, form.get("version"), action="update_settings")
        except (ConfigError, VersionConflict) as exc:
            return back("/settings", request, _errors_text(exc), "error")
        return back("/settings", request, "Settings saved.")

    @app.post("/settings/remove-device")
    async def remove_device(request: Request):
        form = await require_ui_post(request)
        raw, _ = store.read_config()
        name = form.get("name")
        users = [e.get("id") for e in raw.get("events", []) if isinstance(e, dict)
                 and name in ((e.get("action") or {}).get("devices") or {})]
        if users:
            return back("/settings", request, f"'{name}' is used by: {', '.join(users)}. Remove it there first.", "error")
        if name not in raw.get("devices", {}):
            return back("/settings", request, f"No device named '{name}'.", "error")
        del raw["devices"][name]
        try:
            store.save_config(raw, form.get("version"), action="remove_device", detail={"name": name})
        except (ConfigError, VersionConflict) as exc:
            return back("/settings", request, _errors_text(exc), "error")
        return back("/settings", request, f"Removed device '{name}'.")

    # -------------------------------------------------------------- backups

    @app.get("/backups")
    def backups(request: Request):
        require_ui(request)
        return render(request, "backups.html", backups=store.list_backups())

    @app.post("/backups/restore")
    async def restore(request: Request):
        form = await require_ui_post(request)
        try:
            store.restore_backup(form.get("name") or "", form.get("version"))
        except (ConfigError, VersionConflict, NotFound) as exc:
            return back("/backups", request, _errors_text(exc), "error")
        return back("/backups", request, f"Restored {form.get('name')}. The previous config was backed up first.")

    # ============================================================== API

    def api_body(data):
        if not isinstance(data, dict):
            raise ConfigError(["request body must be a JSON object"])
        return data

    async def json_body(request):
        try:
            return api_body(await request.json())
        except ValueError:
            raise ConfigError(["request body must be valid JSON"])

    def expected(request, body=None):
        return request.headers.get("if-match") or (body or {}).get("version")

    @app.get("/api/status")
    def api_status(request: Request):
        api_actor(request)
        raw, version, config = load()
        status = store.read_status()
        now = datetime.now(config.tz)
        nxt = sched.upcoming(config, now, sched.State(), limit=5)
        status["next_computed"] = [{"event": o.event["id"], "when": o.when.isoformat()} for o in nxt]
        status["config_version"] = version
        status["now"] = now.isoformat(timespec="seconds")
        return status

    @app.get("/api/schedule")
    def api_schedule(request: Request, date: str = ""):
        api_actor(request)
        raw, version, config = load()
        day = store.parse_date(date) if date else datetime.now(config.tz).date()
        prayers = store.today_prayer_times(config, day)
        return {"date": day.isoformat(), "prayers": {k: v.isoformat() for k, v in prayers.items()},
                "occurrences": store.schedule_for_day(config, day, store.read_state())}

    @app.get("/api/events")
    def api_events(request: Request):
        api_actor(request)
        raw, version, config = load()
        now = datetime.now(config.tz)
        rows = event_rows(raw, config, now)
        for row in rows:
            row["next"] = row["next"].isoformat() if row["next"] else None
        return {"version": version, "events": raw.get("events", []), "summary": rows}

    @app.post("/api/events", status_code=201)
    async def api_create_event(request: Request):
        actor = api_actor(request)
        body = await json_body(request)
        event = body.get("event", body)
        version = store.create_event(event, expected(request, body), actor)
        return {"version": version, "event": event}

    @app.get("/api/events/{event_id}")
    def api_get_event(request: Request, event_id: str):
        api_actor(request)
        raw, version = store.read_config()
        _, ev = store.find_event(raw, event_id)
        return {"version": version, "event": ev}

    @app.put("/api/events/{event_id}")
    async def api_update_event(request: Request, event_id: str):
        actor = api_actor(request)
        body = await json_body(request)
        event = body.get("event")
        if not isinstance(event, dict):
            raise ConfigError(["body must be {\"event\": {...}, \"version\": \"...\", \"confirm\": bool}"])
        version = store.update_event(event_id, event, expected(request, body), actor,
                                     confirm_prayer=body.get("confirm") is True)
        return {"version": version, "event": event}

    @app.delete("/api/events/{event_id}")
    def api_delete_event(request: Request, event_id: str, confirm: bool = False):
        actor = api_actor(request)
        version = store.delete_event(event_id, request.headers.get("if-match"), actor, confirm_prayer=confirm)
        return {"version": version, "deleted": event_id}

    @app.post("/api/events/{event_id}/skip")
    def api_skip(request: Request, event_id: str):
        actor = api_actor(request)
        raw, _ = store.read_config()
        store.find_event(raw, event_id)
        sched.add_skip(event_id)
        store.audit(actor, "skip_next", {"id": event_id})
        return {"skipped_next": event_id}

    @app.delete("/api/events/{event_id}/skip")
    def api_unskip(request: Request, event_id: str):
        actor = api_actor(request)
        removed = sched.remove_skip(event_id)
        store.audit(actor, "cancel_skip", {"id": event_id})
        return {"cancelled": removed}

    @app.post("/api/events/{event_id}/test", status_code=202)
    def api_test(request: Request, event_id: str):
        actor = api_actor(request)
        raw, _ = store.read_config()
        store.find_event(raw, event_id)
        return store.queue_command("play_event", actor, event_id=event_id)

    @app.post("/api/play", status_code=202)
    async def api_play(request: Request):
        actor = api_actor(request)
        body = await json_body(request)
        raw, version, config = load()
        return store.queue_play_now(body.get("audio"), body.get("devices"), body.get("max_seconds"),
                                    config, actor)

    @app.post("/api/stop", status_code=202)
    async def api_stop(request: Request):
        actor = api_actor(request)
        body = await json_body(request) if (await request.body()) else {}
        raw, version, config = load()
        devices = body.get("devices")
        if devices is not None and (not isinstance(devices, list) or not all(isinstance(d, str) for d in devices)):
            raise ConfigError(["'devices' must be a list of names"])
        return store.queue_stop(devices, config, actor)

    @app.get("/api/commands/{cmd_id}")
    def api_command(request: Request, cmd_id: str):
        api_actor(request)
        return store.command_result(cmd_id)

    @app.get("/api/devices")
    def api_devices(request: Request):
        api_actor(request)
        raw, version, config = load()
        seen = store.read_status().get("devices", {})
        return {"devices": [{"name": n, "enabled": spec.get("enabled", True),
                             "online": (seen.get(n) or {}).get("online"),
                             "checked": (seen.get(n) or {}).get("checked")}
                            for n, spec in config.devices.items()]}

    @app.get("/api/history")
    def api_history(request: Request, since: str = "", device: str = "", status: str = "",
                    event: str = "", limit: int = 200):
        api_actor(request)
        return {"history": store.read_history(max(1, min(limit, 5000)), device or None,
                                              status or None, event or None, since or None)}

    @app.get("/api/audio")
    def api_audio(request: Request):
        api_actor(request)
        raw, version, config = load()
        return {"audio": store.list_audio(config)}

    @app.post("/api/audio", status_code=201)
    async def api_upload(request: Request):
        actor = api_actor(request)
        form = await request.form()
        upload_file = form.get("file")
        if upload_file is None or not getattr(upload_file, "filename", ""):
            raise ConfigError(["multipart field 'file' is required"])
        name = store.save_upload(upload_file.filename, upload_file.file,
                                 overwrite=str(form.get("overwrite", "")).lower() in ("1", "true", "on"),
                                 actor=actor)
        return {"name": name}

    @app.delete("/api/audio/{name}")
    def api_delete_audio(request: Request, name: str):
        actor = api_actor(request)
        raw, version, config = load()
        store.delete_audio(name, config, actor)
        return {"deleted": name}

    @app.get("/api/settings")
    def api_get_settings(request: Request):
        api_actor(request)
        raw, version = store.read_config()
        return {"version": version, "settings": raw.get("settings", {}), "devices": raw.get("devices", {})}

    @app.put("/api/settings")
    async def api_put_settings(request: Request):
        actor = api_actor(request)
        body = await json_body(request)
        raw, _ = store.read_config()
        if "settings" in body:
            if not isinstance(body["settings"], dict):
                raise ConfigError(["'settings' must be an object"])
            raw.setdefault("settings", {}).update(body["settings"])
        if "devices" in body:
            if not isinstance(body["devices"], dict):
                raise ConfigError(["'devices' must be an object"])
            raw["devices"] = body["devices"]
        version = store.save_config(raw, expected(request, body), actor, "update_settings")
        return {"version": version, "settings": raw["settings"], "devices": raw.get("devices", {})}

    @app.get("/api/config/backups")
    def api_backups(request: Request):
        api_actor(request)
        return {"backups": store.list_backups()}

    @app.post("/api/config/restore")
    async def api_restore(request: Request):
        actor = api_actor(request)
        body = await json_body(request)
        version = store.restore_backup(body.get("name"), expected(request, body), actor)
        return {"version": version, "restored": body.get("name")}

    return app


app = create_app()
