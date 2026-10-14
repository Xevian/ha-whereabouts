"""Entity naming and identity checks.

    python tests/test_entity_naming.py

Both entities set has_entity_name, so Home Assistant builds the friendly name
as "<device name> <entity name>", or the device name alone when the entity
name is None, and derives a fresh install's entity_id from that.  The device
name already carries the person, so the entity name must not, or it shows up
twice ("Whereabouts Steve Gare Whereabouts Steve Gare").

The unique_ids are pinned as well: they are what ties existing registry
entries, and so every automation and dashboard, to these entities.
"""

from __future__ import annotations

import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import load  # noqa: E402


def _install_entity_stubs() -> None:
    """The platform modules import a little more of HA than the coordinator."""

    def module(name: str) -> types.ModuleType:
        mod = sys.modules.get(name) or types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    module("homeassistant.components.sensor").SensorEntity = object

    device_tracker = module("homeassistant.components.device_tracker")
    device_tracker.TrackerEntity = object
    device_tracker.SourceType = types.SimpleNamespace(GPS="gps")

    device_registry = module("homeassistant.helpers.device_registry")
    device_registry.DeviceEntryType = types.SimpleNamespace(SERVICE="service")
    device_registry.DeviceInfo = dict

    module("homeassistant.helpers.entity_platform").AddEntitiesCallback = object

    class CoordinatorEntity:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

    module("homeassistant.helpers.update_coordinator").CoordinatorEntity = (
        CoordinatorEntity
    )


_install_entity_stubs()
load("const")
load("zones")
load("geocoder")
load("coordinator")
sensor = load("sensor")
device_tracker = load("device_tracker")

PERSON = "person.steve_gare"


def _friendly_name(entity) -> str:
    """Home Assistant's has_entity_name rule."""
    assert entity._attr_has_entity_name
    device_name = entity.device_info["name"]
    if entity._attr_name is None:
        return device_name
    return f"{device_name} {entity._attr_name}"


def _fresh_entity_id(domain: str, entity) -> str:
    """What a new registry entry would be called (HA slugifies the name)."""
    slug = re.sub(r"[^a-z0-9]+", "_", _friendly_name(entity).lower()).strip("_")
    return f"{domain}.{slug}"


def _sensor():
    return sensor.WhereaboutsSensor(object(), PERSON)


def _tracker():
    return device_tracker.WhereaboutsEventTracker(
        object(), PERSON, "calendar.steve", 150.0
    )


def test_sensor_friendly_name_is_the_device_name():
    assert _friendly_name(_sensor()) == "Whereabouts Steve Gare"


def test_sensor_fresh_entity_id_is_whereabouts_person():
    assert _fresh_entity_id("sensor", _sensor()) == "sensor.whereabouts_steve_gare"


def test_tracker_friendly_name_names_the_person_once():
    assert _friendly_name(_tracker()) == "Whereabouts Steve Gare Next Event"


def test_tracker_fresh_entity_id():
    assert (
        _fresh_entity_id("device_tracker", _tracker())
        == "device_tracker.whereabouts_steve_gare_next_event"
    )


def test_unique_ids_unchanged():
    assert _sensor()._attr_unique_id == "whereabouts_person_steve_gare"
    assert _tracker()._attr_unique_id == "whereabouts_event_person_steve_gare"


def test_both_entities_share_one_device():
    assert _sensor().device_info["identifiers"] == _tracker().device_info["identifiers"]


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
