"""Transit-hub lookup via the Overpass API.

Nominatim cannot answer "which transit hub am I inside": reverse-geocoding a
point in the middle of Birmingham Airport returns the nearest addressable
feature — a service road, or a taxiway holding-position marker — and
`layer=poi` returns no result at all.  Overpass can answer it, because it
queries OSM by tag rather than by address hierarchy.

The trade-off is reliability: public Overpass instances routinely return HTTP
504 under load.  Everything here therefore treats failure as ordinary.  A
lookup that fails returns None, the coordinator keeps whatever it had, and the
sensor falls back to the geocoded city.  Hub data is enrichment; it is never
allowed to degrade city tracking.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    HUB_EXCLUDED_STATION_TYPES,
    HUB_NODE_RADIUS_M,
    OVERPASS_QUERY_TIMEOUT_SECONDS,
    OVERPASS_TIMEOUT_SECONDS,
    OVERPASS_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# OSM tag → the hub_type reported to users.
HUB_TYPE_AIRPORT = "airport"
HUB_TYPE_STATION = "station"
HUB_TYPE_FERRY = "ferry"
HUB_TYPE_BUS = "bus"


@dataclass(frozen=True)
class TransitHub:
    """One transit hub, with whatever geometry OSM has for it."""

    name: str
    hub_type: str
    lat: float
    lon: float
    # (min_lat, max_lat, min_lon, max_lon) for ways/relations; None for nodes.
    bounds: tuple[float, float, float, float] | None = None
    # IATA / CRS / UIC reference, when tagged.
    code: str | None = None

    def contains(self, lat: float, lon: float, node_radius_m: float) -> bool:
        """True if (lat, lon) is inside this hub.

        Airports carry a real bounding box and are tested against it — a
        fixed radius would be wrong by kilometres either way.  Everything
        else is a bare point and gets a radius.
        """
        if self.bounds is not None:
            min_lat, max_lat, min_lon, max_lon = self.bounds
            return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

        return _haversine_m(lat, lon, self.lat, self.lon) <= node_radius_m


class OverpassHubProvider:
    """Fetches transit hubs near a point. Returns None on any failure."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch_hubs(
        self, lat: float, lon: float, radius_m: int
    ) -> list[TransitHub] | None:
        """Return hubs within radius_m of (lat, lon), or None if unavailable.

        None is a meaningful, expected result — it means Overpass could not be
        reached or did not answer — and is distinct from an empty list, which
        means it answered and there is genuinely nothing nearby.
        """
        query = _build_query(lat, lon, radius_m)

        try:
            async with self._session.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=OVERPASS_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    # 504 "server too busy" is the common case and is not
                    # worth a warning every time a busy instance is hit.
                    _LOGGER.debug(
                        "Overpass returned HTTP %s for (%.4f, %.4f) — "
                        "keeping city-only place data",
                        response.status, lat, lon,
                    )
                    return None
                payload: Any = await response.json(content_type=None)

        except asyncio.TimeoutError:
            _LOGGER.debug("Overpass timed out for (%.4f, %.4f)", lat, lon)
            return None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Overpass network error for (%.4f, %.4f): %s", lat, lon, err)
            return None
        except ValueError:
            # Busy instances answer 200 with an HTML error page rather than JSON.
            _LOGGER.debug("Overpass returned a non-JSON body for (%.4f, %.4f)", lat, lon)
            return None
        except Exception:
            _LOGGER.exception("Unexpected error querying Overpass")
            return None

        if not isinstance(payload, dict):
            return None

        remark = payload.get("remark")
        if remark and "error" in str(remark).lower():
            _LOGGER.debug("Overpass reported an error: %s", remark)
            return None

        hubs = _parse_elements(payload.get("elements") or [])
        _LOGGER.debug(
            "Overpass returned %d hubs within %dm of (%.4f, %.4f)",
            len(hubs), radius_m, lat, lon,
        )
        return hubs


def find_hub(
    hubs: list[TransitHub],
    lat: float,
    lon: float,
    node_radius_m: float = HUB_NODE_RADIUS_M,
) -> TransitHub | None:
    """Return the most specific hub containing (lat, lon), or None.

    Where hubs overlap — a station inside an airport's boundary, which is the
    normal arrangement at BHX and BER — the smaller one wins, because it is
    the more precise description of where the person actually is.
    """
    matches = [hub for hub in hubs if hub.contains(lat, lon, node_radius_m)]
    if not matches:
        return None
    return min(matches, key=_area_key)


def _area_key(hub: TransitHub) -> float:
    """Rough extent, used to prefer the smallest containing hub."""
    if hub.bounds is None:
        return 0.0
    min_lat, max_lat, min_lon, max_lon = hub.bounds
    return (max_lat - min_lat) * (max_lon - min_lon)


def _bbox(lat: float, lon: float, radius_m: int) -> str:
    """Overpass [bbox:] string — south,west,north,east — around a point."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return (
        f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
    )


def _build_query(lat: float, lon: float, radius_m: int) -> str:
    """Build the Overpass QL query.

    Filtering happens server-side wherever a tag makes it reliable:

    * Airports must carry an IATA code, which excludes gliding clubs and
      private airstrips tagged aeroway=aerodrome.
    * Stations exclude subway / light_rail / tram / monorail, which is the
      one dependable "this is local transit" signal in OSM and is what keeps
      the London Underground out.

    No attempt is made to select "intercity" stations by tag, because no such
    tag exists in practice — ref:crs covers every UK commuter halt and
    uic_ref covers the Berlin U-Bahn.  Dwell time does that filtering instead
    (see HUB_CONFIRM_DWELL_SECONDS).

    Two choices here are about cost, not correctness, and both come from
    measurement against the public instance rather than guesswork:

    * A global ``[bbox:]`` instead of a per-clause ``around:``. With four
      ``around`` clauses the query reliably exceeded a 50 s server budget —
      each clause costs roughly a fixed ~25 s scan regardless of how little
      it returns, and the aerodrome clause alone took ~26 s to return one
      element. The same query with a global bbox completed in **2 s**.
    * ``node`` rather than ``nwr`` for everything except aerodromes. The 100
      stations around Birmingham come back as nodes either way, but ``nwr``
      cost 28 s against 16 s. Only aerodromes genuinely need way/relation
      geometry, because only they have a meaningful bounding box.
    """
    excluded = "|".join(HUB_EXCLUDED_STATION_TYPES)

    return (
        f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SECONDS}]"
        f"[bbox:{_bbox(lat, lon, radius_m)}];"
        "("
        "nwr[aeroway=aerodrome][name][iata];"
        f'node[railway=station][name][station!~"^({excluded})$"];'
        "node[amenity=ferry_terminal][name];"
        "node[amenity=bus_station][name];"
        ");"
        "out tags center bb;"
    )


def _parse_elements(elements: list[dict[str, Any]]) -> list[TransitHub]:
    """Turn raw Overpass elements into TransitHubs, de-duplicated by name."""
    hubs: list[TransitHub] = []

    for element in elements:
        tags: dict[str, str] = element.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue

        hub_type = _hub_type(tags)
        if hub_type is None:
            continue

        bounds = _bounds_of(element)
        position = _position_of(element, bounds)
        if position is None:
            continue

        hubs.append(
            TransitHub(
                name=name,
                hub_type=hub_type,
                lat=position[0],
                lon=position[1],
                bounds=bounds,
                code=(
                    tags.get("iata")
                    or tags.get("ref:crs")
                    or tags.get("uic_ref")
                    or tags.get("ref")
                ),
            )
        )

    return _deduplicate(hubs)


def _hub_type(tags: dict[str, str]) -> str | None:
    if tags.get("aeroway") == "aerodrome":
        return HUB_TYPE_AIRPORT
    if tags.get("railway") == "station":
        if tags.get("station") in HUB_EXCLUDED_STATION_TYPES:
            return None
        return HUB_TYPE_STATION
    amenity = tags.get("amenity")
    if amenity == "ferry_terminal":
        return HUB_TYPE_FERRY
    if amenity == "bus_station":
        return HUB_TYPE_BUS
    return None


def _bounds_of(element: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = element.get("bounds")
    if not raw:
        return None
    try:
        return (
            float(raw["minlat"]),
            float(raw["maxlat"]),
            float(raw["minlon"]),
            float(raw["maxlon"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _position_of(
    element: dict[str, Any], bounds: tuple[float, float, float, float] | None
) -> tuple[float, float] | None:
    """Representative point: the node itself, the center, or the bbox middle."""
    lat, lon = element.get("lat"), element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        if bounds is None:
            return None
        min_lat, max_lat, min_lon, max_lon = bounds
        return ((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _deduplicate(hubs: list[TransitHub]) -> list[TransitHub]:
    """Collapse duplicate (name, type) entries, keeping the one with geometry.

    Large stations are frequently mapped several times over — Berlin
    Hauptbahnhof's lower platforms are a separate element named
    "Berlin Hauptbahnhof (tief)", and many stations exist as both a node and
    a building way.  Keeping the entry with a bounding box gives the better
    containment test.
    """
    best: dict[tuple[str, str], TransitHub] = {}

    for hub in hubs:
        key = (hub.name, hub.hub_type)
        existing = best.get(key)
        if existing is None:
            best[key] = hub
            continue
        if existing.bounds is None and hub.bounds is not None:
            best[key] = hub
        elif existing.bounds is not None and hub.bounds is not None:
            if _area_key(hub) > _area_key(existing):
                best[key] = hub

    return list(best.values())


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))
