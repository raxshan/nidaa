#!/usr/bin/env python3
"""Run the 13-row staging test matrix against $NIDAA_HOME/staging on the Pi, with real playback.

Deploy first:  ./deploy.sh --staging
Then:          python3 scripts/staging_tests.py --device "Living Room display" [--volume 0.15]

Runs from your computer over SSH (key login; settings from deploy.env). Takes ~17 minutes and only
touches $NIDAA_HOME/staging: the live services are never stopped, restarted or edited.

Safety: prayer events are disabled in staging, max_volume is capped at --volume, every clip is
audio/sound-test.mp3 stopped after 8 s, and the run refuses to start if a real prayer is within
10 minutes before or 30 minutes after now.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
AUDIO = "sound-test.mp3"
DISABLED_DEVICE = "Nidaa disabled test device"   # made-up device, only exists in staging config


def _deploy_env():
    env = {}
    path = ROOT / "deploy.env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_ENV = _deploy_env()
PI = _ENV.get("NIDAA_PI", "pi@raspberrypi.local")
HOME = _ENV.get("NIDAA_HOME", "/home/pi/nidaa")
PY = _ENV.get("NIDAA_VENV", HOME + "/venv") + "/bin/python"
STAGING = HOME + "/staging"
TZ = ZoneInfo("UTC")   # replaced by the staging config's timezone at startup

results = []


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def ssh(cmd, stdin=None, check=True):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", PI, cmd], input=stdin,
                       capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"ssh failed ({r.returncode}): {cmd}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def in_staging(cmd, **kw):
    return ssh(f"cd {STAGING} && {cmd}", **kw)


def remote_py(code):
    return in_staging(f"{PY} -", stdin=code)


def wait_until(when, why):
    secs = (when - datetime.now(TZ)).total_seconds()
    if secs > 0:
        log(f"waiting {int(secs)} s until {when.strftime('%H:%M:%S')} ({why})")
        time.sleep(secs)


def run_log():
    return in_staging("cat run.log 2>/dev/null || true")


def history():
    out = in_staging("cat history.jsonl 2>/dev/null || true")
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def record(num, name, ok, detail=""):
    results.append((num, name, ok, detail))
    log(f"{'PASS' if ok else 'FAIL'}  #{num} {name}  {detail}")


def start_scheduler():
    in_staging(f"(nohup {PY} -u nidaa_scheduler.py >> run.log 2>&1 < /dev/null & echo $! > sched.pid)")
    time.sleep(6)


def stop_scheduler():
    in_staging("if [ -f sched.pid ]; then kill $(cat sched.pid) 2>/dev/null || true; rm -f sched.pid; fi", check=False)
    time.sleep(2)


def set_config(mutate_code):
    """Run python that edits config.json in place (variable c)."""
    remote_py("import json\nc = json.load(open('config.json'))\n" + mutate_code +
              "\njson.dump(c, open('config.json.tmp', 'w'), indent=2)\n"
              "import os; os.replace('config.json.tmp', 'config.json')\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="Cast device name to play the test clips on")
    ap.add_argument("--volume", type=float, default=0.15)
    args = ap.parse_args()
    dev, vol = args.device, args.volume

    global TZ
    if not 0 < vol <= 0.5:
        sys.exit("--volume must be between 0 and 0.5 for tests")

    # ---------------------------------------------------------------- safety gate
    info = json.loads(remote_py(
        "import json, nidaa_scheduler as s\n"
        "from datetime import datetime, timedelta\n"
        "c, _ = s.load_config()\n"
        "now = datetime.now(c.tz)\n"
        "out = []\n"
        "for d in (now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1)):\n"
        "    out += [t.isoformat() for t in s.get_prayer_times(c, d).values()]\n"
        "print(json.dumps({'tz': c.settings['timezone'], 'prayers': out, 'devices': list(c.devices)}))\n"))
    TZ = ZoneInfo(info["tz"])
    if dev not in info["devices"]:
        sys.exit(f"--device {dev!r} is not in the staging config devices: {info['devices']}")
    now = datetime.now(TZ)
    for t in info["prayers"]:
        delta = (datetime.fromisoformat(t) - now).total_seconds()
        if -10 * 60 < delta < 30 * 60:
            log(f"ABORT: a real prayer is at {datetime.fromisoformat(t).strftime('%H:%M')} "
                f"(no prayer allowed from 10 min before now to 30 min after). Nothing was changed.")
            sys.exit(2)

    # The test sound must be in the Pi's audio folder.
    if in_staging(f"test -f ../audio/{AUDIO} && echo yes || echo no").strip() != "yes":
        log(f"copying audio/{AUDIO} to {HOME}/audio/")
        subprocess.run(["scp", "-q", str(ROOT / "audio" / AUDIO), f"{PI}:{HOME}/audio/"], check=True)

    # ---------------------------------------------------------------- timeline
    base = (now + timedelta(minutes=1)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    at = lambda m: base + timedelta(minutes=m)
    hhmm = lambda m: at(m).strftime("%H:%M")
    iso = lambda m: at(m).strftime("%Y-%m-%dT%H:%M")
    wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][at(6).weekday()]

    def ev(eid, trigger, devices=None, volume=None):
        return {"id": eid, "enabled": True, "priority": 10, "trigger": trigger,
                "action": {"audio": AUDIO, "max_seconds": 8,
                           "devices": devices or {dev: volume if volume is not None else vol}}}

    events = [
        ev("test-once", {"type": "once", "datetime": iso(2)}),
        ev("test-yearly", {"type": "yearly", "month": at(4).month, "day": at(4).day, "time": hhmm(4)}),
        ev("test-weekly", {"type": "weekly", "days": [wd], "time": hhmm(6)}),
        ev("test-skipme", {"type": "once", "datetime": iso(8)}),
        ev("test-notfound", {"type": "once", "datetime": iso(10)}, {dev: vol, "Bogus test speaker": vol}),
        ev("test-missed", {"type": "once", "datetime": iso(12)}),
        ev("test-disabled", {"type": "once", "datetime": "2099-01-01T00:00"}, {DISABLED_DEVICE: vol}),
        ev("test-cap", {"type": "once", "datetime": "2099-01-01T00:00"}, volume=0.8),
    ]
    log(f"device={dev!r} volume={vol} base={base.strftime('%H:%M')} (run ends about {at(16).strftime('%H:%M')})")

    stamp = now.strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "inventory" / f"staging-run-{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    try:
        # ------------------------------------------------------------ setup
        stop_scheduler()
        in_staging("rm -f state.json skips.json history.jsonl run.log skips.json.lock config.json.good")
        set_config(
            f"for e in c['events']:\n"
            f"    if e['trigger']['type'] == 'prayer': e['enabled'] = False\n"
            f"c['events'] = [e for e in c['events'] if not e['id'].startswith('test-')]\n"
            f"c['settings']['max_volume'] = {vol}\n"
            f"c['devices'][{DISABLED_DEVICE!r}] = {{'enabled': False}}\n"
            f"c['events'] += {events!r}\n")
        check = in_staging(f"{PY} nidaa_scheduler.py --check", check=False)
        log("--check: " + check.strip().splitlines()[0])
        enabled_prayers = remote_py("import json; c=json.load(open('config.json'));"
                                    "print(sum(1 for e in c['events'] if e['trigger']['type']=='prayer' and e.get('enabled', True)))")
        if enabled_prayers.strip() != "0":
            raise RuntimeError("prayer events still enabled in staging; refusing to run")
        in_staging("cp config.json config.json.good")
        start_scheduler()

        # #4 skip requested well before test-skipme
        in_staging(f"{PY} nidaa_scheduler.py --skip test-skipme")

        # #7 + #8 hot reload: add an event and a bad event while running
        wait_until(at(1), "hot reload")
        set_config(f"c['events'] += {[ev('test-hot', {'type': 'once', 'datetime': iso(9)})]!r}\n"
                   f"c['events'].append({ev('test-bad', {'type': 'weekly', 'days': ['funday'], 'time': '07:00'})!r})\n")
        time.sleep(12)
        lg = run_log()
        record(8, "bad event ignored", "Config problem (event ignored): test-bad" in lg)
        in_staging("cp config.json config.json.good")

        # #1 once fires
        wait_until(at(2) + timedelta(seconds=45), "test-once")
        h = history()
        record(1, "one-off fires", any(r["event"] == "test-once" and r["status"] == "playing" for r in h),
               f"history: {[r['status'] for r in h if r['event'] == 'test-once']}")
        stop_line = "test-once stopped on" in run_log()
        record(1.1, "max_seconds stops playback", stop_line)

        # #5 no replay after restart
        stop_scheduler()
        start_scheduler()
        time.sleep(25)
        n = sum(1 for r in history() if r["event"] == "test-once")
        record(5, "no replay on restart", n == 1, f"test-once history rows: {n}")

        # #3 yearly fires
        wait_until(at(4) + timedelta(seconds=45), "test-yearly")
        record(3, "yearly fires", any(r["event"] == "test-yearly" and r["status"] == "playing" for r in history()))

        # #6a late fire within grace: stopped before, restarted ~60 s after
        wait_until(at(5) + timedelta(seconds=30), "stop before test-weekly")
        stop_scheduler()
        wait_until(at(7), "restart 60 s after test-weekly")
        start_scheduler()
        time.sleep(30)
        lg = run_log()
        record(6, "late fire within grace", "Triggering test-weekly" in lg and
               any(r["event"] == "test-weekly" and r["status"] == "playing" for r in history()))
        record(2, "weekly fires", any(r["event"] == "test-weekly" and r["status"] == "playing" for r in history()))

        # #4 skip consumed
        wait_until(at(8) + timedelta(seconds=30), "test-skipme")
        h = history()
        record(4, "skip next", any(r["event"] == "test-skipme" and r["status"] == "skipped" for r in h)
               and not any(r["event"] == "test-skipme" and r["status"] == "playing" for r in h))

        # #7 hot-reloaded event fired
        wait_until(at(9) + timedelta(seconds=35), "test-hot")
        record(7, "hot reload", "Config reloaded." in run_log() and
               any(r["event"] == "test-hot" and r["status"] == "playing" for r in history()))

        # #9 bad JSON survives, then recovers
        in_staging("python3 -c \"p='config.json'; s=open(p).read(); open(p,'w').write(s.replace(',', '', 1))\"")
        time.sleep(12)
        broke = "Config reload failed, keeping previous config" in run_log()
        in_staging("cp config.json.good config.json.tmp && mv config.json.tmp config.json")
        time.sleep(12)
        recovered = run_log().count("Config reloaded.") >= 2
        record(9, "bad JSON survives", broke and recovered, f"failed-logged={broke} reloaded-after-fix={recovered}")

        # #10 device not found
        wait_until(at(10) + timedelta(seconds=50), "test-notfound")
        h = [r for r in history() if r["event"] == "test-notfound"]
        lg = run_log()
        record(10, "device not found", any(r["device"] == "Bogus test speaker" and r["status"] == "not_found" for r in h)
               and any(r["device"] == dev and r["status"] == "playing" for r in h)
               and "retrying discovery once" in lg, f"{[(r['device'], r['status']) for r in h]}")

        # #6b missed outside grace
        wait_until(at(11) + timedelta(seconds=30), "stop before test-missed")
        stop_scheduler()
        wait_until(at(14) + timedelta(seconds=40), "restart 160 s after test-missed")
        start_scheduler()
        time.sleep(15)
        record(6.1, "missed outside grace", "Missed test-missed" in run_log() and
               not any(r["event"] == "test-missed" for r in history()))

        # #11 disabled device, #12 volume cap, #13 --play
        in_staging(f"{PY} nidaa_scheduler.py --play test-disabled", check=False)
        record(11, "disabled device", any(r["event"] == "test-disabled" and r["status"] == "device_disabled" for r in history()))
        out = in_staging(f"{PY} nidaa_scheduler.py --play test-cap", check=False)
        pct = f"at {int(vol * 100)}%"
        record(12, "volume cap", f"test-cap playing on {dev} {pct}" in out, out.strip().splitlines()[-2:] if out else "")
        out = in_staging(f"{PY} nidaa_scheduler.py --play test-once", check=False)
        record(13, "--play", f"test-once playing on {dev}" in out)

        lg = run_log()
        record(0, "no tracebacks", "Traceback" not in lg)
    finally:
        stop_scheduler()
        (outdir / "run.log").write_text(run_log())
        (outdir / "history.jsonl").write_text(in_staging("cat history.jsonl 2>/dev/null || true", check=False))
        (outdir / "state.json").write_text(in_staging("cat state.json 2>/dev/null || true", check=False))
        (outdir / "config.json").write_text(in_staging("cat config.json 2>/dev/null || true", check=False))
        log(f"staging scheduler stopped; logs saved to {outdir}")

    print()
    print("=" * 60)
    passed = sum(1 for r in results if r[2])
    for num, name, ok, detail in sorted(results, key=lambda r: r[0]):
        print(f"{'PASS' if ok else 'FAIL'}  #{num:<4} {name}  {detail if not ok else ''}")
    print(f"{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
