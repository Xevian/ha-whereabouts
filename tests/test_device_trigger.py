"""Device trigger checks.

    python tests/test_device_trigger.py

The trigger variables handed to an automation must carry the trigger_data HA
passes in (the automation's trigger id and index), or "triggered by" and
`trigger.id` conditions silently never match.  And a trigger whose device has
gone must fire for nobody, not for every tracked person.
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import Hass, load  # noqa: E402


class _Device:
    def __init__(self, identifiers):
        self.identifiers = identifiers


_DEVICES: dict[str, _Device] = {}


def _install_trigger_stubs() -> None:
    def module(name: str) -> types.ModuleType:
        mod = sys.modules.get(name) or types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    # TRIGGER_SCHEMA is built at import time; the attach path never uses it,
    # so a bare stand-in keeps this test on the standard library alone.
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        vol = module("voluptuous")
        vol.Schema = lambda *a, **k: None
        vol.Required = lambda key, **k: key
        vol.In = lambda values: values
        vol.ALLOW_EXTRA = None

    ha_const = module("homeassistant.const")
    ha_const.CONF_DEVICE_ID = "device_id"
    ha_const.CONF_DOMAIN = "domain"
    ha_const.CONF_PLATFORM = "platform"
    ha_const.CONF_TYPE = "type"

    core = sys.modules["homeassistant.core"]
    core.CALLBACK_TYPE = object

    class HassJob:
        def __init__(self, target, name=None):
            self.target = target
            self.name = name

    core.HassJob = HassJob

    module("homeassistant.helpers.typing").ConfigType = dict

    device_registry = module("homeassistant.helpers.device_registry")
    registry = types.SimpleNamespace(async_get=_DEVICES.get)
    device_registry.async_get = lambda hass: registry
    sys.modules["homeassistant.helpers"].device_registry = device_registry


_install_trigger_stubs()
const = load("const")
device_trigger = load("device_trigger")


class _Event:
    def __init__(self, data):
        self.data = data
        self.context = "ctx"


class _TriggerHass(Hass):
    def __init__(self):
        super().__init__()
        self.listeners: dict[str, list] = {}
        self.runs: list[tuple] = []
        self.bus.async_listen = self._listen

    def _listen(self, event_type, callback):
        self.listeners.setdefault(event_type, []).append(callback)
        return lambda: None

    def async_run_hass_job(self, job, *args):
        self.runs.append((job, args))

    def fire(self, event_type, data):
        for callback in self.listeners.get(event_type, []):
            callback(_Event(data))


TRIGGER_INFO = {"trigger_data": {"id": "steve_arrives", "idx": "0"}}


def _attach(hass, device_id):
    import asyncio

    config = {
        "platform": "device",
        "domain": const.DOMAIN,
        "device_id": device_id,
        "type": "city_changed",
    }
    return asyncio.run(
        device_trigger.async_attach_trigger(hass, config, object(), TRIGGER_INFO)
    )


def test_trigger_variables_carry_trigger_data_and_context():
    _DEVICES["dev1"] = _Device({(const.DOMAIN, "person.steve")})
    hass = _TriggerHass()
    _attach(hass, "dev1")

    hass.fire(const.EVENT_CITY_ARRIVED, {const.ATTR_PERSON_ENTITY_ID: "person.steve"})

    assert len(hass.runs) == 1
    _job, (variables, context) = hass.runs[0]
    assert variables["trigger"]["id"] == "steve_arrives"
    assert variables["trigger"]["idx"] == "0"
    assert variables["trigger"]["type"] == "city_changed"
    assert context == "ctx"


def test_trigger_ignores_other_people():
    _DEVICES["dev1"] = _Device({(const.DOMAIN, "person.steve")})
    hass = _TriggerHass()
    _attach(hass, "dev1")

    hass.fire(const.EVENT_CITY_ARRIVED, {const.ATTR_PERSON_ENTITY_ID: "person.jane"})

    assert hass.runs == []


def test_trigger_for_a_missing_device_fires_for_nobody():
    hass = _TriggerHass()
    _attach(hass, "gone")

    hass.fire(const.EVENT_CITY_ARRIVED, {const.ATTR_PERSON_ENTITY_ID: "person.steve"})

    assert hass.runs == []


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, test in tests:
        try:
            test()
        except AssertionError as err:
            failures.append(name)
            print(f"FAIL  {name}\n      {err}")
        except Exception as err:  # noqa: BLE001 - a crash is a failure too
            failures.append(name)
            print("ERROR " + name)
            print("      " + type(err).__name__ + ": " + str(err))
        else:
            print(f"pass  {name}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} failed")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
