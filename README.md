# Nidaa

Nidaa (نداء, "the call") plays the Adhan at the computed prayer times on Google Cast speakers and
smart displays, plus any other scheduled audio you like: weekly reminders, yearly birthdays, one-off
announcements. It runs on a Raspberry Pi and comes with a small, phone-friendly web dashboard for your home network.

- **One deterministic scheduler.** Prayer times are calculated locally with
  [adhanpy](https://pypi.org/project/adhanpy/), so the scheduler needs neither the internet nor the dashboard.
- **Event model:** `prayer` (with an optional offset and weekdays), `weekly`, `yearly`
  (Feb 29 falls back to Feb 28) and `once`, each with per-device audio and volume.
- **Safe by design:** an event plays at most once, even across restarts; a late fire is allowed
  within a grace window; lower-priority events near a prayer give way; there's a global volume cap;
  and the config hot-reloads (a bad edit is rejected and the last good config is kept).
- **Dashboard:** today's times and the next event, a scheduler health badge, an event editor, a
  prayer × device volume matrix, play/stop now, an audio library, history, settings and config backups.
  There's also a REST API for scripts and automations.

## How it fits together

```
nidaa-web.service        python -m http.server 8080   ->  serves audio/ to the Cast devices
nidaa-scheduler.service  app/nidaa_scheduler.py       ->  the only process that talks to Cast devices
nidaa-dashboard.service  app/nidaa_dashboard.py :8081 ->  edits config.json, queues play/stop commands
```

The dashboard never plays audio itself. It writes `config.json` (validated, versioned, backed up and
replaced atomically) and drops commands into `app/commands/`, which the scheduler executes. If the
dashboard is stopped, the scheduler keeps working.

## Requirements

- A Raspberry Pi (or any Linux box) on the same network as your Cast devices, with Python 3.9 or newer
- `pip install pychromecast adhanpy` for the scheduler, plus `-r requirements-dashboard.txt` for the dashboard
- Your own Adhan recordings. Only `audio/sound-test.mp3` ships with this repo.

## Setup

On the Pi:

```bash
mkdir -p ~/nidaa/audio && python3 -m venv ~/nidaa/venv
~/nidaa/venv/bin/pip install pychromecast adhanpy -r requirements-dashboard.txt
avahi-browse -rt _googlecast._tcp | grep fn=      # exact Cast device names
```

On your computer (in this repo):

```bash
cp deploy.env.example deploy.env                  # set NIDAA_PI, NIDAA_USER, NIDAA_HOME, NIDAA_VENV
cp app/config.example.json app/config.json        # location, devices, events, audio_base_url
./deploy.sh --with-config                         # first install; later just ./deploy.sh
scp audio/*.mp3 pi@raspberrypi.local:~/nidaa/audio/
deploy/render-units.sh                            # writes deploy/rendered/*.service
```

Then install the rendered units on the Pi (`/etc/systemd/system/`), create `/etc/nidaa-dashboard.env`
(see below), and run `sudo systemctl daemon-reload && sudo systemctl enable --now nidaa-web nidaa-scheduler nidaa-dashboard`.

`audio_base_url` must be the Pi's **IP address** (for example `http://192.168.1.50:8080/audio`), because
many Cast devices can't resolve `.local` names. Reserve that IP in your router.

## Configuration

`app/config.json` (see `app/config.example.json`):

```json
{
  "settings": { "latitude": 40.7128, "longitude": -74.006, "timezone": "America/New_York",
                "calculation_method": "NORTH_AMERICA", "max_volume": 0.8, "...": "..." },
  "devices":  { "Kitchen speaker": { "enabled": true } },
  "events": [
    { "id": "prayer-fajr", "trigger": { "type": "prayer", "prayer": "Fajr" },
      "action": { "audio": "sound-test.mp3", "devices": { "Kitchen speaker": 0.25 } } },
    { "id": "suhoor", "trigger": { "type": "prayer", "prayer": "Fajr", "offset_minutes": -45 },
      "active_from": "2027-02-08", "active_until": "2027-03-09",
      "action": { "audio": "sound-test.mp3", "max_seconds": 30, "devices": { "Kitchen speaker": 0.3 } } }
  ]
}
```

- **Per-device values:** each device takes either a volume (0–1) or `{"audio": "sound-test.mp3", "volume": 0.4}`.
- **Optional event keys:** `enabled`, `priority` (defaults: prayer 100, once 60, yearly 50, weekly 40),
  `active_from` / `active_until`, and `action.max_seconds`.
- **Optional settings:** `madhab` (`SHAFI`, `HANAFI`) and `high_latitude_rule`
  (`MIDDLE_OF_THE_NIGHT`, `SEVENTH_OF_THE_NIGHT`, `TWILIGHT_ANGLE`).

CLI (use the venv's Python):

```bash
python nidaa_scheduler.py --check            # validate config.json (and find missing audio)
python nidaa_scheduler.py --today            # today's schedule
python nidaa_scheduler.py --skip EVENT_ID    # skip the next occurrence
python nidaa_scheduler.py --play EVENT_ID    # play an event now
```

## Dashboard and API

Open `http://<pi-ip>:8081` from any device on your network. `/etc/nidaa-dashboard.env` holds:

```ini
NIDAA_DASHBOARD_PASSWORD=a-long-passphrase
NIDAA_SESSION_SECRET=<python3 -c "import secrets; print(secrets.token_urlsafe(32))">
NIDAA_API_TOKEN=<another random value>
```

Logins use a signed `HttpOnly` / `SameSite=Strict` cookie, every write is CSRF-checked, failed logins are
rate-limited, and the server enforces volume limits and validates uploads itself. The API (`/api/status`,
`/api/schedule`, `/api/events`, `/api/play`, `/api/stop`, `/api/history`, `/api/audio`, `/api/settings`,
`/api/config/backups`, …) accepts `Authorization: Bearer $NIDAA_API_TOKEN`. Writes accept an `If-Match`
version and return `409` if the config changed in the meantime.

**Keep it on your LAN.** Don't port-forward it. For remote access, use a VPN such as Tailscale.

## Testing

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv -r requirements-dev.txt pychromecast adhanpy
.venv/bin/python -m pytest -q
```

The tests stub Cast and prayer-time calculation, so no device is needed. To test with real playback on
the Pi, without touching the live services:

```bash
./deploy.sh --staging
python3 scripts/staging_tests.py --device "Kitchen speaker" --volume 0.15
```

This plays `sound-test.mp3` for 8 s at a time, keeps prayer events off, and won't start near a prayer time.

## License

[MIT](LICENSE). This covers the code and `audio/sound-test.mp3`. Bring your own Adhan recordings, and check their licences before sharing them.
