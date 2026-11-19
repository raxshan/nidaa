"""Test setup: stub pychromecast and adhanpy so tests never touch the network
or depend on the real libraries, then import the app modules from app/."""

import enum
import json
import sys
import types
from datetime import datetime, time as dtime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))


# ---------------------------------------------------------------- adhanpy stub

# Fixed local prayer times; tests can override per date via PRAYER_OVERRIDES.
DEFAULT_TIMES = {
    "fajr": dtime(5, 30), "dhuhr": dtime(12, 30), "asr": dtime(15, 30),
    "maghrib": dtime(18, 0), "isha": dtime(19, 30),
}
PRAYER_OVERRIDES = {}   # {date: {"fajr": time, ...}}
PRAYER_CALLS = []       # records how PrayerTimes was constructed


class _FakePrayerTimes:
    def __init__(self, coordinates, date, calculation_method=None,
                 calculation_parameters=None, time_zone=None):
        PRAYER_CALLS.append({"method": calculation_method, "params": calculation_parameters})
        day = date.date()
        times = {**DEFAULT_TIMES, **PRAYER_OVERRIDES.get(day, {})}
        for name, t in times.items():
            setattr(self, name, datetime.combine(day, t, tzinfo=time_zone))


class _CalculationMethod(enum.Enum):
    NORTH_AMERICA = 1
    MUSLIM_WORLD_LEAGUE = 2


class _Madhab(enum.Enum):
    SHAFI = 0
    HANAFI = 1


class _HighLatitudeRule(enum.Enum):
    MIDDLE_OF_THE_NIGHT = 0
    SEVENTH_OF_THE_NIGHT = 1
    TWILIGHT_ANGLE = 2


class _CalculationParameters:
    def __init__(self, method=None):
        self.method = method
        self.madhab = _Madhab.SHAFI
        self.high_latitude_rule = _HighLatitudeRule.MIDDLE_OF_THE_NIGHT


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


_module("adhanpy")
_module("adhanpy.PrayerTimes", PrayerTimes=_FakePrayerTimes)
_module("adhanpy.calculation", CalculationMethod=_CalculationMethod)
_module("adhanpy.calculation.CalculationParameters", CalculationParameters=_CalculationParameters)
_module("adhanpy.calculation.HighLatitudeRule", HighLatitudeRule=_HighLatitudeRule)
_module("adhanpy.calculation.Madhab", Madhab=_Madhab)


# ----------------------------------------------------------- pychromecast stub

class FakeMedia:
    def __init__(self, cast):
        self.cast = cast

    def play_media(self, url, mime):
        self.cast.calls.append(("play", url, mime))

    def block_until_active(self, timeout=None):
        pass

    def stop(self):
        self.cast.calls.append(("stop",))


class FakeCast:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.volume = None
        self.media_controller = FakeMedia(self)

    def wait(self, timeout=None):
        pass

    def set_volume(self, v):
        self.volume = v
        self.calls.append(("volume", v))

    def disconnect(self):
        pass


CASTS = {}           # name -> FakeCast currently "on the network"
DISCOVERY_CALLS = []


def _get_chromecasts(**kwargs):
    DISCOVERY_CALLS.append(kwargs)
    return list(CASTS.values()), object()


_discovery = types.SimpleNamespace(stop_discovery=lambda browser: None)
_module("pychromecast", get_chromecasts=_get_chromecasts, discovery=_discovery)


# -------------------------------------------------------------------- fixtures

import nidaa_scheduler as sched  # noqa: E402

SAMPLE_CONFIG = json.loads((ROOT / "app" / "config.example.json").read_text())
# Tests use a fixed 0.8 cap regardless of what the example config says.
SAMPLE_CONFIG["settings"]["max_volume"] = 0.8


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Point every scheduler file at a temp dir, with a copy of the real config."""
    audio = tmp_path / "audio"
    audio.mkdir()
    for name in ("sound-test.mp3", "sound-test-alt.mp3"):
        (audio / name).write_bytes(b"ID3" + b"\0" * 64)
    app = tmp_path / "app"
    app.mkdir()
    (app / "config.json").write_text(json.dumps(SAMPLE_CONFIG, indent=2))
    monkeypatch.setattr(sched, "CONFIG_PATH", app / "config.json")
    monkeypatch.setattr(sched, "STATE_PATH", app / "state.json")
    monkeypatch.setattr(sched, "SKIPS_PATH", app / "skips.json")
    monkeypatch.setattr(sched, "HISTORY_PATH", app / "history.jsonl")
    monkeypatch.setattr(sched, "STATUS_PATH", app / "status.json", raising=False)
    monkeypatch.setattr(sched, "COMMANDS_DIR", app / "commands", raising=False)
    monkeypatch.setattr(sched, "AUDIO_DIR", audio)
    CASTS.clear()
    DISCOVERY_CALLS.clear()
    PRAYER_OVERRIDES.clear()
    PRAYER_CALLS.clear()
    return app


@pytest.fixture
def casts():
    """Put the three household devices on the fake network."""
    for name in ("Bedroom display", "Living Room display", "Kitchen speaker"):
        CASTS[name] = FakeCast(name)
    return CASTS


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
