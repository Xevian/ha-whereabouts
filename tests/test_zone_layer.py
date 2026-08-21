"""Tests for the zone / place layer.

Runs standalone with the standard library alone:

    python tests/test_zone_layer.py

and is also discoverable by pytest — every test is a plain sync function that
drives the async code via asyncio.run(), so no pytest-asyncio is needed.

The subject under test is the real coordinator and the real async_setup_entry
listener; only Home Assistant itself is stubbed (see stub_hass).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import Hass, State, load, reset_clock, tick  # noqa: E402

const = load("const")
load("zones")
geocoder = load("geocoder")
coordinator_module = load("coordinator")
whereabouts_init = load("__init__")

PERSON = "person.steve"

GLOUCESTER = {
    "city": "Gloucester",
    "boundingbox": ["51.83", "51.90", "-2.30", "-2.19"],
    "osm_id": 1,
    "place_type": "city",
    "country": "United Kingdom",
    "country_code": "GB",
}
BRISTOL = {
    "city": "Bristol",
    "boundingbox": ["51.40", "51.50", "-2.65", "-2.50"],
    "osm_id": 2,
    "place_type": "city",
    "country": "United Kingdom",
    "country_code": "GB",
}

# Which geocode result the stubbed Nominatim returns next.
_GEOCODE_RESULT = [GLOUCESTER]


async def _fake_reverse_geocode(self, lat, lon):
    return _GEOCODE_RESULT[0]


geocoder.NominatimGeocoder.reverse_geocode = _fake_reverse_geocode


async def _no_calendar_event(*args, **kwargs):
    return None


# ── fixtures ──────────────────────────────────────────────────────────────
def build_coordinator(passive_zones: dict[str, dict] | None = None):
    """A coordinator with one person and a Home zone in Gloucester."""
    reset_clock()
    _GEOCODE_RESULT[0] = GLOUCESTER

    hass = Hass()
    hass.states.set(
        State(
            "zone.home",
            "1",
            {
                "latitude": 51.865,
                "longitude": -2.245,
                "radius": 100,
                "friendly_name": "Home",
            },
        )
    )
    for entity_id, attributes in (passive_zones or {}).items():
        hass.states.set(State(entity_id, "0", attributes))

    coordinator = coordinator_module.WhereaboutsCoordinator(
        hass, [PERSON], geocode_cooldown_seconds=0
    )
    coordinator.data = {}
    coordinator._check_calendar_proximity = lambda *a, **k: _no_calendar_event()
    return hass, coordinator


def place_of(coordinator):
    """(place, place_source, city, zone) for the tracked person."""
    entry = coordinator.data[PERSON]
    return (
        entry.get("place"),
        entry.get("place_source"),
        entry.get("city"),
        entry.get("zone"),
    )


def zone_events(hass):
    """Zone events fired since the last drain, as 'event:zone' strings."""
    fired = [
        f"{name}:{data.get('zone')}"
        for name, data in hass.bus.fired
        if "zone" in name
    ]
    hass.bus.fired.clear()
    return fired


# ── listener harness ──────────────────────────────────────────────────────
_SCHEDULED: list = []


async def setup_listener():
    """Run async_setup_entry against stubs, returning its state-change callback."""
    captured: dict = {}

    # __init__ did `from ... import async_track_state_change_event`, binding the
    # function at import time, so the patch has to land on the module attribute.
    whereabouts_init.async_track_state_change_event = (
        lambda hass, ids, cb: captured.setdefault("cb", cb)
    )

    reset_clock()
    _GEOCODE_RESULT[0] = GLOUCESTER

    hass = Hass()
    hass.states.set(
        State(
            "zone.home",
            "1",
            {
                "latitude": 51.865,
                "longitude": -2.245,
                "radius": 100,
                "friendly_name": "Home",
            },
        )
    )
    hass.states.set(
        State(PERSON, "not_home", {"latitude": 51.870, "longitude": -2.250})
    )
    # Skip the bundled-icons static path registration, which needs hass.http.
    hass.data[f"{const.DOMAIN}_static_registered"] = True

    def create_task(coro):
        task = asyncio.ensure_future(coro)
        _SCHEDULED.append(task)
        return task

    hass.async_create_task = create_task

    class Entry:
        entry_id = "test"
        data = {const.CONF_PERSONS: [PERSON], const.CONF_SCAN_INTERVAL: 0}

        def async_on_unload(self, func):
            pass

        def add_update_listener(self, func):
            pass

    class ConfigEntries:
        async def async_forward_entry_setups(self, entry, platforms):
            pass

    hass.config_entries = ConfigEntries()

    await whereabouts_init.async_setup_entry(hass, Entry())

    coordinator = hass.data[const.DOMAIN]["test"]
    coordinator._check_calendar_proximity = lambda *a, **k: _no_calendar_event()
    hass.bus.fired.clear()
    return captured["cb"], hass, coordinator


async def deliver(listener, old_state, new_state):
    """Deliver one state_changed event and drain whatever work it scheduled."""

    class Event:
        data = {
            "entity_id": PERSON,
            "old_state": old_state,
            "new_state": new_state,
        }

    listener(Event())
    if _SCHEDULED:
        await asyncio.gather(*_SCHEDULED)
        _SCHEDULED.clear()


# ── tests ─────────────────────────────────────────────────────────────────
def test_zone_outranks_city_and_preserves_it():
    """At home in Gloucester: state says Home, city attribute still Gloucester."""

    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(PERSON, 51.865, -2.245, "home")
        assert place_of(coordinator) == ("Home", "zone", "Gloucester", "Home")
        assert zone_events(hass) == ["whereabouts_zone_arrived:Home"]

    asyncio.run(run())


def test_leaving_zone_falls_back_to_city():
    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(PERSON, 51.865, -2.245, "home")
        zone_events(hass)

        tick(600)
        await coordinator.async_handle_location_update(
            PERSON, 51.870, -2.250, "not_home"
        )
        assert place_of(coordinator) == ("Gloucester", "city", "Gloucester", None)
        assert zone_events(hass) == ["whereabouts_zone_departed:Home"]

    asyncio.run(run())


def test_named_zone_reported_verbatim():
    """Any person state that isn't home/not_home is already the zone name."""

    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(PERSON, 51.872, -2.252, "Work")
        assert place_of(coordinator)[:2] == ("Work", "zone")

    asyncio.run(run())


def test_zone_update_without_coordinates():
    """Phones drop GPS on home Wi-Fi; the zone is still knowable from state."""

    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(
            PERSON, 51.870, -2.250, "not_home"
        )
        tick(600)
        await coordinator.async_handle_zone_update(PERSON, "home")
        assert place_of(coordinator)[:3] == ("Home", "zone", "Gloucester")

    asyncio.run(run())


def test_zone_survives_moving_state():
    """A bbox exit sets state=moving, but an active zone still names the place."""

    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(PERSON, 51.865, -2.245, "home")

        _GEOCODE_RESULT[0] = BRISTOL
        tick(600)
        await coordinator.async_handle_location_update(PERSON, 51.45, -2.58, "home")

        entry = coordinator.data[PERSON]
        assert entry["state"] in ("moving", "Bristol")
        assert (entry["place"], entry["place_source"]) == ("Home", "zone")

    asyncio.run(run())


def test_calendar_event_outranks_zone():
    async def run():
        hass, coordinator = build_coordinator()

        async def at_event(*args, **kwargs):
            return "MCM Comic Con"

        coordinator._check_calendar_proximity = at_event
        await coordinator.async_handle_location_update(PERSON, 51.865, -2.245, "home")

        assert place_of(coordinator)[:2] == ("MCM Comic Con", "calendar")
        # The zone is still recorded underneath, just outranked.
        assert coordinator.data[PERSON]["zone"] == "Home"

    asyncio.run(run())


def test_passive_zone_matched_by_distance():
    """HA excludes passive zones from person state, so they need a distance check."""

    async def run():
        hass, coordinator = build_coordinator(
            passive_zones={
                "zone.allotment": {
                    "latitude": 51.8700,
                    "longitude": -2.2600,
                    "radius": 150,
                    "passive": True,
                    "friendly_name": "Allotment",
                }
            }
        )
        await coordinator.async_handle_location_update(
            PERSON, 51.8701, -2.2601, "not_home"
        )
        assert place_of(coordinator)[:2] == ("Allotment", "zone")

    asyncio.run(run())


def test_previous_zone_tracked():
    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(PERSON, 51.865, -2.245, "home")
        tick(600)
        await coordinator.async_handle_location_update(PERSON, 51.868, -2.248, "Work")
        assert coordinator.data[PERSON]["previous_zone"] == "Home"

    asyncio.run(run())


def test_no_zones_leaves_behaviour_unchanged():
    """Regression guard: without zones, nothing about the old behaviour moves."""

    async def run():
        hass, coordinator = build_coordinator()
        await coordinator.async_handle_location_update(
            PERSON, 51.870, -2.250, "not_home"
        )
        assert place_of(coordinator)[:2] == ("Gloucester", "city")
        assert zone_events(hass) == []

    asyncio.run(run())


def test_listener_keeps_zone_change_with_identical_coordinates():
    """The whole feature hinges on this event not being dropped as a no-op."""

    async def run():
        listener, hass, coordinator = await setup_listener()
        old = State(PERSON, "not_home", {"latitude": 51.865, "longitude": -2.245})
        new = State(PERSON, "home", {"latitude": 51.865, "longitude": -2.245})
        await deliver(listener, old, new)
        assert place_of(coordinator)[:2] == ("Home", "zone")

    asyncio.run(run())


def test_listener_still_drops_true_noop_updates():
    """Regression guard: unchanged state *and* unchanged coords is still noise."""

    async def run():
        listener, hass, coordinator = await setup_listener()
        before = len(_SCHEDULED)
        old = State(PERSON, "not_home", {"latitude": 51.870, "longitude": -2.250})
        new = State(PERSON, "not_home", {"latitude": 51.870, "longitude": -2.250})
        await deliver(listener, old, new)
        assert len(_SCHEDULED) - before == 0

    asyncio.run(run())


def test_listener_routes_gpsless_zone_change():
    """Tracker drops GPS entirely: still routed to the zone-only path."""

    async def run():
        listener, hass, coordinator = await setup_listener()
        await coordinator.async_handle_location_update(
            PERSON, 51.870, -2.250, "not_home"
        )
        old = State(PERSON, "not_home", {"latitude": 51.870, "longitude": -2.250})
        new = State(PERSON, "home", {})
        await deliver(listener, old, new)
        assert place_of(coordinator)[:3] == ("Home", "zone", "Gloucester")

    asyncio.run(run())


# ── standalone runner ─────────────────────────────────────────────────────
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
            failures.append((name, err))
            print(f"FAIL  {name}\n      {err}")
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
