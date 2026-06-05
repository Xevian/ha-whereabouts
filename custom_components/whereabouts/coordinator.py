"""DataUpdateCoordinator for Whereabouts."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_BEARING,
    ATTR_CALENDAR_EVENT,
    ATTR_CITY,
    ATTR_COUNTRY,
    ATTR_COUNTRY_CODE,
    ATTR_DIRECTION,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_PERSON_ENTITY_ID,
    ATTR_PREVIOUS_CITY,
    ATTR_PREVIOUS_COUNTRY,
    ATTR_SPEED,
    ATTR_SPEED_MPH,
    DOMAIN,
    EVENT_CALENDAR_ARRIVED,
    EVENT_CALENDAR_DEPARTED,
    EVENT_CITY_ARRIVED,
    EVENT_CITY_DEPARTED,
    EVENT_COUNTRY_ARRIVED,
    EVENT_COUNTRY_DEPARTED,
    EVENT_STARTED_MOVING,
    MAX_BBOX_DEGREES,
    STATE_MOVING,
    STATE_UNKNOWN,
)
from .geocoder import NominatimGeocoder

_LOGGER = logging.getLogger(__name__)


class WhereaboutsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages city/country state for all tracked persons.

    Location updates are pushed in from __init__.py via
    async_handle_location_update() — one call per state-change event on
    each person entity.  No periodic polling occurs (update_interval=None).

    Geocoding is rate-limited per person: at most one Nominatim call per
    geocode_cooldown_seconds, controlled by the "scan interval" config option.

    coordinator.data shape:
        {
            "person.john": {
                "city": "Paris" | None,
                "boundingbox": ["48.815", "48.902", "2.224", "2.470"] | None,
                "osm_id": 7444 | None,
                "place_type": "city" | None,
                "latitude": 48.8566 | None,
                "longitude": 2.3522 | None,
                "state": "Paris" | "moving" | "unknown",
                "previous_city": "Lyon" | None,
                "country": "France" | None,
                "country_code": "FR" | None,
                "previous_country": "Spain" | None,
            },
            ...
        }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        person_entity_ids: list[str],
        geocode_cooldown_seconds: int,
        person_calendars: dict[str, str | None] | None = None,
        event_radius_m: float = 300,
    ) -> None:
        self._person_entity_ids = person_entity_ids
        self._geocoder = NominatimGeocoder(async_get_clientsession(hass))
        self._geocode_cooldown_seconds = geocode_cooldown_seconds

        # Per-person bounding-box / city cache.
        self._cache: dict[str, dict[str, Any]] = {}
        # Timestamps of the last successful geocode call, keyed by entity_id.
        self._last_geocode: dict[str, datetime] = {}
        # Previous GPS fix per person: (lat, lon, utc_datetime).
        # Used to compute speed and bearing between consecutive updates.
        self._prev_fix: dict[str, tuple[float, float, datetime]] = {}

        # Per-person calendar mapping: person_entity_id → calendar_entity_id | None.
        # A None value means no calendar is linked for that person.
        self._person_calendars: dict[str, str | None] = person_calendars or {}
        self._event_radius_m = event_radius_m
        # Cache of forward-geocoded calendar locations: text → (lat, lon).
        # Entries persist for the life of the coordinator (HA session).
        self._event_location_cache: dict[str, tuple[float, float]] = {}
        # Which calendar event title each person is currently at (for detecting transitions).
        self._at_event: dict[str, str | None] = {}
        # Pending city arrivals — geocoded but not yet confirmed by a subsequent
        # GPS update inside the same bbox.  Discarded if the person moves on
        # before the next update, preventing drive-through "arrival" spam.
        self._pending_arrival: dict[str, dict[str, Any]] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # Event-driven; no polling.
        )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def async_initialize(self) -> None:
        """Seed initial state and geocode persons that already have GPS coords.

        Called once from async_setup_entry so sensors are never stuck on
        STATE_UNKNOWN at startup if the person already has a location.
        """
        # Start everyone as unknown so the sensor platform has data to show.
        self.async_set_updated_data(
            {eid: _unknown_state() for eid in self._person_entity_ids}
        )

        for entity_id in self._person_entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            lat = state.attributes.get("latitude")
            lon = state.attributes.get("longitude")
            if lat is not None and lon is not None:
                await self.async_handle_location_update(
                    entity_id, float(lat), float(lon)
                )


    # ------------------------------------------------------------------
    # Core location-update handler (called by state-change listener)
    # ------------------------------------------------------------------

    async def async_handle_location_update(
        self, entity_id: str, lat: float, lon: float
    ) -> None:
        """Process a new GPS fix for one person.

        1. Compute speed + bearing from the previous fix (if any).
        2. If inside cached bbox → update coords + motion, keep city name, no API call.
        3. If outside bbox → immediately set state to 'moving', then geocode
           only if the per-person cooldown has expired.
        """
        now = dt_util.utcnow()
        cached = self._cache.get(entity_id)

        # ── Motion: speed + bearing from previous fix ─────────────────────
        prev = self._prev_fix.get(entity_id)
        speed_kmh, speed_mph, bearing, direction = _compute_motion(prev, lat, lon, now)
        self._prev_fix[entity_id] = (lat, lon, now)

        # ── Calendar event proximity (takes display priority over city) ───
        event_title = await self._check_calendar_proximity(entity_id, lat, lon)
        prev_event = self._at_event.get(entity_id)
        self._at_event[entity_id] = event_title

        if event_title != prev_event:
            if event_title:
                self._fire_calendar_arrived(entity_id, event_title, lat, lon)
            elif prev_event:
                self._fire_calendar_departed(entity_id, prev_event, lat, lon)

        # ── Fast path: still inside the cached bounding box ──────────────
        if (
            cached
            and cached.get("boundingbox")
            and _within_bbox(lat, lon, cached["boundingbox"])
        ):
            cached["latitude"] = lat
            cached["longitude"] = lon
            cached["state"] = cached["city"]
            cached[ATTR_SPEED] = speed_kmh
            cached[ATTR_SPEED_MPH] = speed_mph
            cached[ATTR_BEARING] = bearing
            cached[ATTR_DIRECTION] = direction
            cached[ATTR_CALENDAR_EVENT] = event_title
            self._push(entity_id, cached)

            # Confirm any pending arrival — person is still here on this update.
            if entity_id in self._pending_arrival:
                p = self._pending_arrival.pop(entity_id)
                _LOGGER.debug(
                    "%s: confirmed arrival in %s (pending cleared)", entity_id, p["city"]
                )
                self._fire_arrived(
                    entity_id, p["city"], p["old_city"],
                    p["country"], p["country_code"], lat, lon,
                )
            return

        # ── Outside bbox: go to 'moving' immediately ─────────────────────
        # Discard any pending arrival — person left before the next bbox
        # confirmation, so they were just passing through.
        if entity_id in self._pending_arrival:
            discarded = self._pending_arrival.pop(entity_id)
            _LOGGER.debug(
                "%s: discarding pending arrival in %s (left bbox before confirmation)",
                entity_id, discarded["city"],
            )

        old_state = (cached or {}).get("state")
        moving = _moving_state(lat, lon, cached, speed_kmh, speed_mph, bearing, direction, event_title)
        self._cache[entity_id] = moving
        self._push(entity_id, moving)

        # Fire started_moving only on the transition (city/unknown → moving),
        # not on every subsequent GPS update while already moving.
        if old_state not in (STATE_MOVING, STATE_UNKNOWN, None):
            self._fire_started_moving(entity_id, lat, lon)

        # ── Geocoding cooldown check ──────────────────────────────────────
        last = self._last_geocode.get(entity_id)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < self._geocode_cooldown_seconds:
                _LOGGER.debug(
                    "%s: geocoding cooldown active (%.0fs remaining)",
                    entity_id,
                    self._geocode_cooldown_seconds - elapsed,
                )
                return

        # ── Call Nominatim ────────────────────────────────────────────────
        self._last_geocode[entity_id] = now
        geo = await self._geocoder.reverse_geocode(lat, lon)

        if geo is None:
            # No settlement found — stay 'moving'.
            return

        new_city: str = geo["city"]
        new_country: str | None = geo["country"]
        new_country_code: str | None = geo["country_code"]
        old_city: str | None = cached.get("city") if cached else None
        old_country: str | None = cached.get("country") if cached else None

        entry: dict[str, Any] = {
            "city": new_city,
            "boundingbox": _cap_bbox(geo["boundingbox"], lat, lon),
            "osm_id": geo["osm_id"],
            "place_type": geo["place_type"],
            "latitude": lat,
            "longitude": lon,
            "state": new_city,
            "previous_city": old_city,
            "country": new_country,
            "country_code": new_country_code,
            "previous_country": old_country,
            ATTR_SPEED: speed_kmh,
            ATTR_SPEED_MPH: speed_mph,
            ATTR_BEARING: bearing,
            ATTR_DIRECTION: direction,
            ATTR_CALENDAR_EVENT: event_title,
        }
        self._cache[entity_id] = entry
        self._push(entity_id, entry)

        # ── City events ───────────────────────────────────────────────────
        if old_city is not None and old_city != new_city:
            # Departed fires immediately — person has definitely left.
            self._fire_departed(entity_id, old_city, old_country, lat, lon)
            # Arrived is held as pending until the next GPS update confirms
            # the person is still inside this bbox (filters out drive-throughs).
            self._pending_arrival[entity_id] = {
                "city": new_city,
                "old_city": old_city,
                "country": new_country,
                "country_code": new_country_code,
            }
            _LOGGER.debug(
                "%s: pending arrival in %s (waiting for bbox confirmation)", entity_id, new_city
            )
        elif old_city is None:
            # First detection at startup — fire immediately, no confirmation needed.
            self._fire_arrived(entity_id, new_city, None, new_country, new_country_code, lat, lon)

        # ── Country events ────────────────────────────────────────────────
        if old_country is not None and old_country != new_country:
            self._fire_country_departed(entity_id, old_country, lat, lon)
            self._fire_country_arrived(entity_id, new_country, new_country_code, old_country, lat, lon)
        elif old_country is None and new_country is not None:
            self._fire_country_arrived(entity_id, new_country, new_country_code, None, lat, lon)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_calendar_proximity(
        self, entity_id: str, lat: float, lon: float
    ) -> str | None:
        """Return the active calendar event title if person is within event_radius_m of its location.

        Each person has their own calendar configured via CONF_PERSON_CALENDARS.
        Reads the HA calendar entity state (state="on" means an event is active).
        The event's location text is forward-geocoded via Nominatim and cached
        for the HA session — so only one API call is ever made per unique string.
        """
        calendar_entity_id = self._person_calendars.get(entity_id)
        if not calendar_entity_id:
            return None

        cal = self.hass.states.get(calendar_entity_id)
        if cal is None or cal.state != "on":
            return None

        location: str | None = cal.attributes.get("location")
        title: str | None = cal.attributes.get("message")

        if not location or not title:
            return None

        # Forward-geocode the location string (cached after first lookup).
        coords = self._event_location_cache.get(location)
        if coords is None:
            coords = await self._geocoder.geocode_address(location)
            if coords is None:
                _LOGGER.warning(
                    "Could not geocode calendar event location %r for %s — "
                    "event proximity check skipped",
                    location,
                    entity_id,
                )
                return None
            _LOGGER.debug("Cached event location %r → %s", location, coords)
            self._event_location_cache[location] = coords

        event_lat, event_lon = coords
        distance_m = _haversine_m(lat, lon, event_lat, event_lon)

        if distance_m <= self._event_radius_m:
            _LOGGER.debug(
                "%s within %.0f m of event %r (radius %.0f m)",
                entity_id,
                distance_m,
                title,
                self._event_radius_m,
            )
            return title

        return None

    def _fire_calendar_arrived(
        self, entity_id: str, event_title: str, lat: float, lon: float
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_CALENDAR_ARRIVED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_CALENDAR_EVENT: event_title,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s arrived at event %r", EVENT_CALENDAR_ARRIVED, entity_id, event_title)

    def _fire_calendar_departed(
        self, entity_id: str, event_title: str, lat: float, lon: float
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_CALENDAR_DEPARTED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_CALENDAR_EVENT: event_title,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s departed event %r", EVENT_CALENDAR_DEPARTED, entity_id, event_title)

    def _push(self, entity_id: str, entry: dict[str, Any]) -> None:
        """Merge one person's entry into coordinator.data and notify sensors."""
        current = dict(self.data) if self.data else {}
        current[entity_id] = dict(entry)
        self.async_set_updated_data(current)

    async def _async_update_data(self) -> dict[str, Any]:
        """Not auto-called (update_interval=None). Returns current data unchanged."""
        return self.data or {eid: _unknown_state() for eid in self._person_entity_ids}

    # ------------------------------------------------------------------
    # Event firing
    # ------------------------------------------------------------------

    def _fire_started_moving(
        self, entity_id: str, lat: float, lon: float
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_STARTED_MOVING,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s started moving", EVENT_STARTED_MOVING, entity_id)

    def _fire_departed(
        self,
        entity_id: str,
        city: str,
        country: str | None,
        lat: float,
        lon: float,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_CITY_DEPARTED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_CITY: city,
                ATTR_COUNTRY: country,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s departed %s", EVENT_CITY_DEPARTED, entity_id, city)

    def _fire_arrived(
        self,
        entity_id: str,
        city: str,
        previous_city: str | None,
        country: str | None,
        country_code: str | None,
        lat: float,
        lon: float,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_CITY_ARRIVED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_CITY: city,
                ATTR_PREVIOUS_CITY: previous_city,
                ATTR_COUNTRY: country,
                ATTR_COUNTRY_CODE: country_code,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s arrived in %s", EVENT_CITY_ARRIVED, entity_id, city)

    def _fire_country_departed(
        self,
        entity_id: str,
        country: str,
        lat: float,
        lon: float,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_COUNTRY_DEPARTED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_COUNTRY: country,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s departed %s", EVENT_COUNTRY_DEPARTED, entity_id, country)

    def _fire_country_arrived(
        self,
        entity_id: str,
        country: str | None,
        country_code: str | None,
        previous_country: str | None,
        lat: float,
        lon: float,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_COUNTRY_ARRIVED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_COUNTRY: country,
                ATTR_COUNTRY_CODE: country_code,
                ATTR_PREVIOUS_COUNTRY: previous_country,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s arrived in %s", EVENT_COUNTRY_ARRIVED, entity_id, country)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _cap_bbox(
    boundingbox: list[str] | None,
    lat: float,
    lon: float,
) -> list[str] | None:
    """Return the bbox unchanged if small, or replace it with a fixed-size box.

    Nominatim sometimes returns the boundary of a large administrative area
    (e.g. Greater London, Cotswold District) whose bbox can span 50+ km.
    Capping it to MAX_BBOX_DEGREES centred on the user's GPS guarantees a
    re-geocode after at most ~11 km of movement.
    """
    if boundingbox is None:
        return None
    try:
        min_lat, max_lat, min_lon, max_lon = (float(v) for v in boundingbox)
    except (TypeError, ValueError):
        return None

    if (
        (max_lat - min_lat) <= MAX_BBOX_DEGREES * 2
        and (max_lon - min_lon) <= MAX_BBOX_DEGREES * 2
    ):
        return boundingbox  # Already small enough — keep as-is.

    _LOGGER.debug(
        "Capping oversized bbox (%.3f°×%.3f°) to %.2f° centred on (%.5f, %.5f)",
        max_lat - min_lat,
        max_lon - min_lon,
        MAX_BBOX_DEGREES,
        lat,
        lon,
    )
    return [
        str(lat - MAX_BBOX_DEGREES),
        str(lat + MAX_BBOX_DEGREES),
        str(lon - MAX_BBOX_DEGREES),
        str(lon + MAX_BBOX_DEGREES),
    ]


def _within_bbox(lat: float, lon: float, boundingbox: list[str]) -> bool:
    """Return True if (lat, lon) is inside a Nominatim bounding box.

    Nominatim format: [min_lat, max_lat, min_lon, max_lon] — all strings.
    """
    try:
        min_lat, max_lat, min_lon, max_lon = (float(v) for v in boundingbox)
    except (TypeError, ValueError):
        return False
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two GPS points."""
    r = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _compute_motion(
    prev: tuple[float, float, datetime] | None,
    lat: float,
    lon: float,
    now: datetime,
) -> tuple[float | None, float | None, float | None, str | None]:
    """Return (speed_kmh, speed_mph, bearing_degrees, direction_text) from two GPS fixes.

    Returns (None, None, None, None) when there is no previous fix, the time
    delta is zero, or the calculated speed exceeds 1 000 km/h (implausible / stale).
    """
    if prev is None:
        return None, None, None, None

    prev_lat, prev_lon, prev_time = prev
    elapsed = (now - prev_time).total_seconds()
    if elapsed <= 0:
        return None, None, None, None

    # Haversine distance (km)
    r = 6371.0
    phi1, phi2 = math.radians(prev_lat), math.radians(lat)
    dphi = math.radians(lat - prev_lat)
    dlambda = math.radians(lon - prev_lon)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    distance_km = 2 * r * math.asin(math.sqrt(a))
    speed_kmh = (distance_km / elapsed) * 3600

    if speed_kmh > 1000:
        # Implausible — likely a large time gap after phone was offline.
        return None, None, None, None

    # Bearing (0° = North, clockwise)
    y = math.sin(dlambda) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    bearing_deg = (math.degrees(math.atan2(y, x)) + 360) % 360

    # 8-point compass rose
    _compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    direction = _compass[int((bearing_deg + 22.5) / 45) % 8]

    speed_mph = round(speed_kmh * 0.621371, 1)

    return round(speed_kmh, 1), speed_mph, round(bearing_deg, 1), direction


def _unknown_state() -> dict[str, Any]:
    return {
        "city": None,
        "boundingbox": None,
        "osm_id": None,
        "place_type": None,
        "latitude": None,
        "longitude": None,
        "state": STATE_UNKNOWN,
        "previous_city": None,
        "country": None,
        "country_code": None,
        "previous_country": None,
        ATTR_SPEED: None,
        ATTR_SPEED_MPH: None,
        ATTR_BEARING: None,
        ATTR_DIRECTION: None,
        ATTR_CALENDAR_EVENT: None,
    }


def _moving_state(
    lat: float,
    lon: float,
    cached: dict[str, Any] | None,
    speed_kmh: float | None = None,
    speed_mph: float | None = None,
    bearing: float | None = None,
    direction: str | None = None,
    event_title: str | None = None,
) -> dict[str, Any]:
    """State used when person is outside their cached city boundary."""
    return {
        "city": None,
        "boundingbox": None,
        "osm_id": None,
        "place_type": None,
        "latitude": lat,
        "longitude": lon,
        "state": STATE_MOVING,
        "previous_city": cached.get("city") if cached else None,
        # Preserve last known country while in transit — useful for templates.
        "country": cached.get("country") if cached else None,
        "country_code": cached.get("country_code") if cached else None,
        "previous_country": cached.get("previous_country") if cached else None,
        ATTR_SPEED: speed_kmh,
        ATTR_SPEED_MPH: speed_mph,
        ATTR_BEARING: bearing,
        ATTR_DIRECTION: direction,
        ATTR_CALENDAR_EVENT: event_title,
    }
