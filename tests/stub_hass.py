"""Minimal Home Assistant stubs, so the integration can be exercised offline.

Whereabouts talks to a small, well-defined slice of Home Assistant: an entity
state machine, an event bus, a DataUpdateCoordinator base class and two utility
functions.  Faking that slice is far cheaper than installing HA, and it makes
the tests deterministic — the clock is manual, so dwell windows can be crossed
without waiting for them.

Everything here is a stub of HA, never of Whereabouts: the code under test is
the real module, imported unmodified.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import types
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(REPO_ROOT, "custom_components", "whereabouts")

# ── manual clock ──────────────────────────────────────────────────────────
_EPOCH = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
_CLOCK = [_EPOCH]


def tick(seconds: float) -> None:
    """Advance the fake clock, e.g. to cross an arrival dwell window."""
    _CLOCK[0] += timedelta(seconds=seconds)


def reset_clock() -> None:
    _CLOCK[0] = _EPOCH


# ── fake state machine / bus ──────────────────────────────────────────────
class State:
    """Stand-in for homeassistant.core.State."""

    def __init__(self, entity_id: str, state: str, attributes: dict | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.name = entity_id.split(".")[-1].replace("_", " ").title()


class Bus:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, data: dict) -> None:
        self.fired.append((event_type, data))


class States:
    def __init__(self):
        self._states: dict[str, State] = {}

    def set(self, state: State) -> None:
        self._states[state.entity_id] = state

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    def async_all(self, domain: str) -> list[State]:
        return [s for eid, s in self._states.items() if eid.startswith(f"{domain}.")]


class Hass:
    """Stand-in for homeassistant.core.HomeAssistant."""

    def __init__(self):
        self.states = States()
        self.bus = Bus()
        self.data: dict = {}


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle metres — matches homeassistant.util.location.distance."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _install() -> None:
    """Register the fake homeassistant / aiohttp modules. Idempotent."""
    if "homeassistant" in sys.modules:
        return

    aiohttp = _module("aiohttp")
    aiohttp.ClientSession = object
    aiohttp.ClientError = Exception
    aiohttp.ClientTimeout = lambda **kwargs: None

    ha = _module("homeassistant")
    ha.__path__ = []

    core = _module("homeassistant.core")
    core.HomeAssistant = Hass
    core.Event = object
    core.callback = lambda func: func

    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = object

    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []

    aiohttp_client = _module("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None

    event = _module("homeassistant.helpers.event")
    event.async_track_state_change_event = lambda hass, ids, cb: None

    update_coordinator = _module("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        """Only the surface Whereabouts actually uses."""

        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        def async_set_updated_data(self, data) -> None:
            self.data = data

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator

    util = _module("homeassistant.util")
    util.__path__ = []
    dt_util = _module("homeassistant.util.dt")
    dt_util.utcnow = lambda: _CLOCK[0]
    util.dt = dt_util
    location = _module("homeassistant.util.location")
    location.distance = _distance


def load(module_name: str) -> types.ModuleType:
    """Import one whereabouts module by path.

    Loaded directly rather than as `custom_components.whereabouts.<name>`
    because the repo has no `custom_components/__init__.py` — HACS integrations
    are not packages in the importable sense.
    """
    _install()
    if "wa" not in sys.modules:
        package = types.ModuleType("wa")
        package.__path__ = [PACKAGE_DIR]
        sys.modules["wa"] = package

    full_name = f"wa.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(
        full_name, os.path.join(PACKAGE_DIR, f"{module_name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_install()
