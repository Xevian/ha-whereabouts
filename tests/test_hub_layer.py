"""Tests for the transit-hub layer.

    python tests/test_hub_layer.py

No network access: the Overpass payloads below are the real response shapes
captured from live queries around Birmingham and Berlin, so the parser and the
geometry are exercised against data OSM actually returns.

The guarantee these tests exist to protect is that hubs are *enrichment*. Any
Overpass failure must leave city tracking byte-for-byte as it would have been.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import Hass, State, load, reset_clock, tick  # noqa: E402

const = load("const")
hubs_module = load("hubs")
load("zones")
geocoder = load("geocoder")
coordinator_module = load("coordinator")

TransitHub = hubs_module.TransitHub
find_hub = hubs_module.find_hub

PERSON = "person.steve"

# ── real Overpass element shapes ──────────────────────────────────────────
# Birmingham Airport: a relation with a genuine bounding box measuring
# 2.90 km x 2.29 km. A fixed radius would be wrong by kilometres.
BHX_ELEMENT = {
    "type": "relation",
    "id": 2437117,
    "bounds": {
        "minlat": 52.44132,
        "maxlat": 52.46745,
        "minlon": -1.76381,
        "maxlon": -1.72999,
    },
    "tags": {
        "aeroway": "aerodrome",
        "aerodrome": "international",
        "iata": "BHX",
        "icao": "EGBB",
        "name": "Birmingham Airport",
    },
}
# Birmingham International: a bare node, as almost every station is.
BHI_ELEMENT = {
    "type": "node",
    "id": 6715981413,
    "lat": 52.4507760,
    "lon": -1.7253182,
    "tags": {
        "railway": "station",
        "name": "Birmingham International",
        "ref:crs": "BHI",
        "operator": "Avanti West Coast",
    },
}
TUBE_ELEMENT = {
    "type": "node",
    "id": 111,
    "lat": 52.4500,
    "lon": -1.7300,
    "tags": {
        "railway": "station",
        "station": "subway",
        "name": "Some Underground Stop",
        "uic_ref": "8000000",
    },
}
# Berlin Hbf is mapped twice: the main element and the lower-level platforms.
BERLIN_HBF_NODE = {
    "type": "node",
    "id": 222,
    "lat": 52.5250,
    "lon": 13.3694,
    "tags": {"railway": "station", "name": "Berlin Hauptbahnhof", "uic_ref": "8011160"},
}
BERLIN_HBF_WAY = {
    "type": "way",
    "id": 333,
    "bounds": {
        "minlat": 52.5240,
        "maxlat": 52.5260,
        "minlon": 13.3680,
        "maxlon": 13.3710,
    },
    "tags": {"railway": "station", "name": "Berlin Hauptbahnhof", "uic_ref": "8011160"},
}
FERRY_ELEMENT = {
    "type": "node",
    "id": 444,
    "lat": 51.1279,
    "lon": 1.3350,
    "tags": {"amenity": "ferry_terminal", "name": "Dover Eastern Docks"},
}


# ── query construction ────────────────────────────────────────────────────
def test_query_excludes_local_transit():
    """The tube must never appear; station=subway is the one reliable signal."""
    query = hubs_module._build_query(52.4536, -1.7433, 25000)
    assert "subway" in query and "light_rail" in query
    assert "station!~" in query
    # Airports need an IATA code, keeping gliding clubs out.
    assert "[aeroway=aerodrome][name][iata]" in query
    assert query.startswith("[out:json][timeout:")
    assert query.endswith("out tags center bb;")


def test_query_is_bbox_scoped_and_node_only_except_airports():
    """Both of these are load-bearing for the query completing at all.

    Per-clause `around:` made the query exceed a 50 s server budget; a global
    bbox runs it in ~2 s. `nwr` costs roughly double `node` for the same
    result, and only aerodromes need way/relation geometry.
    """
    query = hubs_module._build_query(52.4536, -1.7433, 25000)

    assert "[bbox:" in query, "must be bbox-scoped, not around-scoped"
    assert "around:" not in query

    assert "nwr[aeroway=aerodrome]" in query
    for clause in ("[railway=station]", "[amenity=ferry_terminal]",
                   "[amenity=bus_station]"):
        assert f"node{clause}" in query, f"{clause} should be node-only"
        assert f"nwr{clause}" not in query


def test_bbox_brackets_the_requested_radius():
    south, west, north, east = (
        float(v) for v in hubs_module._bbox(52.4536, -1.7433, 25000).split(",")
    )
    assert south < 52.4536 < north
    assert west < -1.7433 < east
    # 25 km is ~0.2246 degrees of latitude; longitude is wider at this latitude.
    assert abs((north - south) / 2 - 0.2246) < 0.01
    assert (east - west) > (north - south)


# ── parsing ───────────────────────────────────────────────────────────────
def test_parse_assigns_types_and_codes():
    parsed = hubs_module._parse_elements([BHX_ELEMENT, BHI_ELEMENT, FERRY_ELEMENT])
    by_name = {hub.name: hub for hub in parsed}

    assert by_name["Birmingham Airport"].hub_type == "airport"
    assert by_name["Birmingham Airport"].code == "BHX"
    assert by_name["Birmingham Airport"].bounds is not None

    assert by_name["Birmingham International"].hub_type == "station"
    assert by_name["Birmingham International"].code == "BHI"
    # Stations are nodes: no extent, so containment falls back to a radius.
    assert by_name["Birmingham International"].bounds is None

    assert by_name["Dover Eastern Docks"].hub_type == "ferry"


def test_parse_drops_subway_even_if_the_server_returns_it():
    """Belt and braces: the tag filter is enforced client-side too."""
    parsed = hubs_module._parse_elements([TUBE_ELEMENT, BHI_ELEMENT])
    assert [hub.name for hub in parsed] == ["Birmingham International"]


def test_parse_drops_unnamed_elements():
    unnamed = {"type": "node", "id": 9, "lat": 1.0, "lon": 1.0,
               "tags": {"railway": "station"}}
    assert hubs_module._parse_elements([unnamed]) == []


def test_duplicate_names_collapse_keeping_geometry():
    """Berlin Hbf is mapped as both a node and a way; the way has the bbox."""
    parsed = hubs_module._parse_elements([BERLIN_HBF_NODE, BERLIN_HBF_WAY])
    assert len(parsed) == 1
    assert parsed[0].bounds is not None


# ── geometry ──────────────────────────────────────────────────────────────
def test_airport_uses_bounding_box_not_radius():
    airport = hubs_module._parse_elements([BHX_ELEMENT])[0]

    # The terminal, ~1 km from the centroid, is inside.
    assert airport.contains(52.4536, -1.7433, node_radius_m=250)
    # The far end of the runway is still inside.
    assert airport.contains(52.4420, -1.7620, node_radius_m=250)
    # A point beyond the perimeter is not.
    assert not airport.contains(52.4800, -1.7433, node_radius_m=250)


def test_station_uses_radius():
    station = hubs_module._parse_elements([BHI_ELEMENT])[0]
    # ~100 m north of the node.
    assert station.contains(52.45168, -1.7253182, node_radius_m=250)
    # ~450 m north is outside a 250 m radius.
    assert not station.contains(52.45483, -1.7253182, node_radius_m=250)


def test_find_hub_prefers_the_more_specific_hub():
    """A station inside an airport boundary should win over the airport."""
    airport = hubs_module._parse_elements([BHX_ELEMENT])[0]
    inside_station = TransitHub(
        name="Airport Shuttle Station",
        hub_type="station",
        lat=52.4536,
        lon=-1.7433,
    )
    found = find_hub([airport, inside_station], 52.4536, -1.7433)
    assert found.name == "Airport Shuttle Station"


def test_find_hub_returns_none_when_outside_everything():
    airport = hubs_module._parse_elements([BHX_ELEMENT])[0]
    assert find_hub([airport], 51.8642, -2.2380) is None


# ── provider failure modes ────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status=200, payload=None, raise_value_error=False):
        self.status = status
        self._payload = payload
        self._raise = raise_value_error

    async def json(self, content_type=None):
        if self._raise:
            raise ValueError("not json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _fetch(session):
    provider = hubs_module.OverpassHubProvider(session)
    return asyncio.run(provider.fetch_hubs(52.4536, -1.7433, 25000))


def test_provider_returns_none_on_server_busy():
    """HTTP 504 is the single most common Overpass outcome under load."""
    assert _fetch(FakeSession(FakeResponse(status=504))) is None


def test_provider_returns_none_on_html_error_body():
    assert _fetch(FakeSession(FakeResponse(raise_value_error=True))) is None


def test_provider_returns_none_on_timeout():
    assert _fetch(FakeSession(error=asyncio.TimeoutError())) is None


def test_provider_returns_none_on_overpass_remark_error():
    payload = {"elements": [], "remark": "runtime error: query timed out"}
    assert _fetch(FakeSession(FakeResponse(payload=payload))) is None


def test_provider_distinguishes_empty_from_unavailable():
    """An empty list means 'nothing here'; None means 'could not ask'."""
    payload = {"elements": []}
    assert _fetch(FakeSession(FakeResponse(payload=payload))) == []


def test_provider_parses_a_successful_response():
    payload = {"elements": [BHX_ELEMENT, BHI_ELEMENT, TUBE_ELEMENT]}
    result = _fetch(FakeSession(FakeResponse(payload=payload)))
    assert sorted(hub.name for hub in result) == [
        "Birmingham Airport",
        "Birmingham International",
    ]


# ── coordinator integration ───────────────────────────────────────────────
# Hubs placed inside the stub city's bounding box so the coordinator's fast
# path behaves as it would in the field.
GLOUCESTER = {
    "city": "Gloucester",
    "boundingbox": ["51.83", "51.90", "-2.30", "-2.19"],
    "osm_id": 1,
    "place_type": "city",
    "country": "United Kingdom",
    "country_code": "GB",
}
STATION = TransitHub(
    name="Gloucester Station", hub_type="station", lat=51.8650, lon=-2.2450, code="GCR"
)
AIRPORT = TransitHub(
    name="Gloucestershire Airport",
    hub_type="airport",
    lat=51.8940,
    lon=-2.1670,
    bounds=(51.8880, 51.9000, -2.1800, -2.1540),
    code="GLO",
)


async def _fake_reverse_geocode(self, lat, lon):
    return GLOUCESTER


geocoder.NominatimGeocoder.reverse_geocode = _fake_reverse_geocode


async def _no_calendar_event(*args, **kwargs):
    return None


class StubHubProvider:
    """Stands in for Overpass. `result=None` simulates the server being down."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def fetch_hubs(self, lat, lon, radius_m):
        self.calls += 1
        return self.result


def build_coordinator(hub_result=None, track_hubs=True):
    reset_clock()
    hass = Hass()
    coordinator = coordinator_module.WhereaboutsCoordinator(
        hass, [PERSON], geocode_cooldown_seconds=0, track_hubs=track_hubs
    )
    coordinator.data = {}
    coordinator._check_calendar_proximity = lambda *a, **k: _no_calendar_event()
    provider = StubHubProvider(hub_result)
    coordinator._hub_provider = provider
    return hass, coordinator, provider


def place_of(coordinator):
    entry = coordinator.data[PERSON]
    return entry.get("place"), entry.get("place_source"), entry.get("city")


def hub_events(hass):
    fired = [
        f"{name}:{data.get('hub')}"
        for name, data in hass.bus.fired
        if "hub" in name
    ]
    hass.bus.fired.clear()
    return fired


def test_hub_arrival_requires_dwell():
    """Arriving is not enough; you have to still be there."""

    async def run():
        hass, coordinator, _ = build_coordinator([STATION, AIRPORT])

        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        # Inside the station radius, but only just arrived.
        assert place_of(coordinator)[:2] == ("Gloucester", "city")
        assert hub_events(hass) == []

        tick(const.HUB_CONFIRM_DWELL_SECONDS + 10)
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        assert place_of(coordinator) == (
            "Gloucester Station",
            "hub",
            "Gloucester",
        )
        assert hub_events(hass) == ["whereabouts_hub_arrived:Gloucester Station"]

    asyncio.run(run())


def test_crossing_a_hub_at_speed_is_not_an_arrival():
    """Driving across an airport is not arriving at it, however long it takes.

    Uses the airport rather than the station because only a hub with real
    extent can hold someone inside it for the whole dwell window while they
    are still plainly in transit.
    """

    async def run():
        hass, coordinator, _ = build_coordinator([STATION, AIRPORT])

        # Enter the airport boundary.
        await coordinator.async_handle_location_update(PERSON, 51.8890, -2.1790)
        assert hub_events(hass) == []

        # Dwell window satisfied, still inside the boundary — but ~1.9 km was
        # covered in 200 s, so this is a drive-through, not an arrival.
        tick(const.HUB_CONFIRM_DWELL_SECONDS + 20)
        await coordinator.async_handle_location_update(PERSON, 51.8990, -2.1560)

        entry = coordinator.data[PERSON]
        assert entry["speed_kmh"] >= const.HUB_CONFIRM_SPEED_KMH, (
            f"fixture must exceed the speed gate, got {entry['speed_kmh']}"
        )
        assert hub_events(hass) == []
        assert entry["place_source"] == "city"

        # Now stop. Same place, dwell already satisfied, speed drops to zero.
        tick(200)
        await coordinator.async_handle_location_update(PERSON, 51.8990, -2.1560)
        assert hub_events(hass) == ["whereabouts_hub_arrived:Gloucestershire Airport"]
        assert place_of(coordinator)[:2] == ("Gloucestershire Airport", "hub")

    asyncio.run(run())


def test_hub_departure_fires():
    async def run():
        hass, coordinator, _ = build_coordinator([STATION, AIRPORT])
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        tick(const.HUB_CONFIRM_DWELL_SECONDS + 10)
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        hub_events(hass)

        # Move well clear of the station, still inside Gloucester.
        tick(600)
        await coordinator.async_handle_location_update(PERSON, 51.8500, -2.2200)
        assert hub_events(hass) == ["whereabouts_hub_departed:Gloucester Station"]
        assert place_of(coordinator)[:2] == ("Gloucester", "city")
        assert coordinator.data[PERSON]["previous_hub"] == "Gloucester Station"

    asyncio.run(run())


def test_overpass_failure_leaves_city_intact():
    """The core guarantee: hubs are enrichment and may never degrade the city."""

    async def run():
        hass, coordinator, provider = build_coordinator(hub_result=None)

        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        tick(const.HUB_CONFIRM_DWELL_SECONDS + 10)
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)

        assert provider.calls >= 1, "Overpass should have been attempted"
        assert place_of(coordinator) == ("Gloucester", "city", "Gloucester")
        assert hub_events(hass) == []
        assert coordinator.data[PERSON]["hub"] is None

    asyncio.run(run())


def test_hubs_disabled_never_calls_overpass():
    async def run():
        hass, coordinator, provider = build_coordinator(
            [STATION, AIRPORT], track_hubs=False
        )
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        tick(const.HUB_CONFIRM_DWELL_SECONDS + 10)
        await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)

        assert provider.calls == 0
        assert place_of(coordinator)[:2] == ("Gloucester", "city")

    asyncio.run(run())


def test_zone_outranks_hub():
    """A user-drawn zone beats the OSM name for the same spot."""

    async def run():
        hass, coordinator, _ = build_coordinator([STATION, AIRPORT])
        hass.states.set(
            State(
                "zone.home",
                "1",
                {
                    "latitude": 51.8650,
                    "longitude": -2.2450,
                    "radius": 100,
                    "friendly_name": "Home",
                },
            )
        )
        await coordinator.async_handle_location_update(
            PERSON, 51.8650, -2.2450, "home"
        )
        tick(const.HUB_CONFIRM_DWELL_SECONDS + 10)
        await coordinator.async_handle_location_update(
            PERSON, 51.8650, -2.2450, "home"
        )

        place, source, city = place_of(coordinator)
        assert (place, source) == ("Home", "zone")
        assert city == "Gloucester"
        # The hub is still recorded underneath, just outranked.
        assert coordinator.data[PERSON]["hub"] == "Gloucester Station"

    asyncio.run(run())


def test_city_is_published_before_the_hub_lookup():
    """A slow or dead Overpass must never delay the city reaching the sensor.

    The provider blocks until released, so if the city were published after
    the hub lookup, there would be no city on the sensor at that point.
    """

    async def run():
        hass, coordinator, _ = build_coordinator([STATION, AIRPORT])

        released = asyncio.Event()
        observed: list = []

        class BlockingProvider:
            calls = 0

            async def fetch_hubs(self, lat, lon, radius_m):
                type(self).calls += 1
                # Snapshot what the sensor is showing while Overpass hangs.
                observed.append(place_of(coordinator))
                await released.wait()
                return [STATION, AIRPORT]

        coordinator._hub_provider = BlockingProvider()

        task = asyncio.ensure_future(
            coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        )
        # Let the handler run up to the point where it blocks on Overpass.
        for _ in range(20):
            await asyncio.sleep(0)
            if observed:
                break

        assert observed, "hub provider was never reached"
        assert observed[0] == ("Gloucester", "city", "Gloucester"), (
            f"city should already be published, saw {observed[0]}"
        )

        released.set()
        await task

    asyncio.run(run())


def test_hub_list_is_cached_per_city():
    """Overpass is called once per city, not once per GPS update."""

    async def run():
        hass, coordinator, provider = build_coordinator([STATION, AIRPORT])
        for _ in range(5):
            tick(60)
            await coordinator.async_handle_location_update(PERSON, 51.8650, -2.2450)
        assert provider.calls == 1

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
