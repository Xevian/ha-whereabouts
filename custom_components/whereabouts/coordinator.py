"""DataUpdateCoordinator for Whereabouts."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
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
    ATTR_HUB,
    ATTR_HUB_CODE,
    ATTR_HUB_TYPE,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_PERSON_ENTITY_ID,
    ATTR_PLACE,
    ATTR_PLACE_SOURCE,
    ATTR_PREVIOUS_CITY,
    ATTR_PREVIOUS_COUNTRY,
    ATTR_PREVIOUS_HUB,
    ATTR_PREVIOUS_ZONE,
    ATTR_ZONE,
    ATTR_SPEED,
    ATTR_SPEED_MPH,
    ARRIVAL_CONFIRM_DWELL_SECONDS,
    ARRIVAL_CONFIRM_SPEED_KMH,
    DOMAIN,
    MIN_BBOX_DEGREES,
    EVENT_CALENDAR_ARRIVED,
    EVENT_CALENDAR_DEPARTED,
    EVENT_CITY_ARRIVED,
    EVENT_CITY_DEPARTED,
    EVENT_COUNTRY_ARRIVED,
    EVENT_COUNTRY_DEPARTED,
    EVENT_HUB_ARRIVED,
    EVENT_HUB_DEPARTED,
    EVENT_STARTED_MOVING,
    EVENT_ZONE_ARRIVED,
    EVENT_ZONE_DEPARTED,
    HUB_CACHE_MAX_CITIES,
    HUB_CONFIRM_DWELL_SECONDS,
    HUB_CONFIRM_SPEED_KMH,
    HUB_NODE_RADIUS_M,
    HUB_SEARCH_RADIUS_M,
    MAX_BBOX_DEGREES,
    PLACE_SOURCE_CALENDAR,
    PLACE_SOURCE_CITY,
    PLACE_SOURCE_HUB,
    PLACE_SOURCE_MOVING,
    PLACE_SOURCE_UNKNOWN,
    PLACE_SOURCE_ZONE,
    STATE_MOVING,
    STATE_UNKNOWN,
)
from .geocoder import NominatimGeocoder
from .hubs import OverpassHubProvider, TransitHub, find_hub
from .zones import resolve_zone

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
                "zone": "Home" | None,
                "hub": "Birmingham Airport" | None,
                "hub_type": "airport" | "station" | "ferry" | "bus" | None,
                "place": "Home" | "Paris" | "moving",
                "place_source": "zone" | "calendar" | "hub" | "city" | "moving",
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
        track_hubs: bool = False,
        config_entry: ConfigEntry | None = None,
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
        # Which HA zone each person is currently inside, and the last
        # different one.  Unlike city arrivals these need no dwell/speed
        # confirmation: HA has already decided the person is in the zone,
        # and its own zone logic accounts for GPS accuracy.
        self._zone: dict[str, str | None] = {}
        self._previous_zone: dict[str, str | None] = {}

        # ── Transit hubs ─────────────────────────────────────────────
        # Pure enrichment: every failure path here leaves city tracking
        # exactly as it would have been.
        self._track_hubs = track_hubs
        self._hub_provider = OverpassHubProvider(async_get_clientsession(hass))
        # Hubs near each geocoded place, keyed by the city's osm_id so one
        # Overpass call serves every subsequent GPS update in that city.
        self._hub_cache: dict[int | str, list[TransitHub]] = {}
        # The hub list currently in play for each person — a reference into
        # _hub_cache, refreshed when they geocode into a different city.
        self._active_hubs: dict[str, list[TransitHub]] = {}
        # Which hub each person is currently at, and the last different one.
        self._hub: dict[str, TransitHub | None] = {}
        self._previous_hub: dict[str, str | None] = {}
        # Hub arrivals awaiting dwell confirmation.
        self._pending_hub: dict[str, dict[str, Any]] = {}
        # Pending city arrivals — geocoded but not yet confirmed by a subsequent
        # GPS update inside the same bbox.  Discarded if the person moves on
        # before the next update, preventing drive-through "arrival" spam.
        self._pending_arrival: dict[str, dict[str, Any]] = {}
        # Last city announced via a (confirmed) arrived event.  Unlike the
        # cache (which is wiped to city=None during 'moving' spells), this
        # survives GPS-drift excursions, so re-geocoding the same place is
        # not mistaken for a fresh arrival — while a genuine return after a
        # confirmed departure still announces properly.
        self._confirmed_city: dict[str, str | None] = {}
        # Last *different* confirmed city — exposed as previous_city.
        self._previous_city: dict[str, str | None] = {}
        # Persons whose departure from _confirmed_city has already fired,
        # so drive-through geocodes can't fire duplicate departed events.
        self._departure_announced: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # Event-driven; no polling.
            # Passed explicitly rather than left to DataUpdateCoordinator's
            # ContextVar fallback, which is deprecated and only works because
            # this happens to be constructed inside async_setup_entry.
            config_entry=config_entry,
        )

    def has_pending_arrival(self, entity_id: str) -> bool:
        """True while an arrival is waiting for its confirming GPS update."""
        return entity_id in self._pending_arrival

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
                    entity_id, float(lat), float(lon), state.state
                )
            else:
                # No GPS (router-based tracker, or a phone settled on Wi-Fi at
                # home).  The zone is still knowable from the person's state,
                # and is the only place name available for them.
                await self.async_handle_zone_update(entity_id, state.state)


    # ------------------------------------------------------------------
    # Core location-update handler (called by state-change listener)
    # ------------------------------------------------------------------

    async def async_handle_location_update(
        self, entity_id: str, lat: float, lon: float, person_state: str | None = None
    ) -> None:
        """Process a new GPS fix for one person.

        1. Compute speed + bearing from the previous fix (if any).
        2. Resolve the HA zone (if any) from person_state — free, no geometry.
        3. If inside cached bbox → update coords + motion, keep city name, no API call.
        4. If outside bbox → immediately set state to 'moving', then geocode
           only if the per-person cooldown has expired.

        person_state is the raw person entity state, used to resolve the zone.
        Geocoding is unaffected by it: `city` is always the real geocoded city
        even while `place` reports a zone name on top of it.
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

        # ── HA zone (takes display priority over city, below calendar) ────
        zone_name = resolve_zone(self.hass, person_state, lat, lon)
        self._update_zone(entity_id, zone_name, lat, lon)

        # ── Transit hub (below zone, above city) ──────────────────────────
        # Runs against the cached hub list only — no network call here.
        hub = self._update_hub(entity_id, lat, lon, speed_kmh, now)

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
            self._set_place(entity_id, cached, event_title, zone_name, hub)
            self._push(entity_id, cached)

            # Confirm any pending arrival — person is still here on this update,
            # is travelling below the "clearly driving through" threshold, AND
            # has been here long enough that this isn't a stop-start jam.
            if entity_id in self._pending_arrival:
                pending = self._pending_arrival[entity_id]
                still_moving = (
                    speed_kmh is not None
                    and speed_kmh >= ARRIVAL_CONFIRM_SPEED_KMH
                )
                dwell = (now - pending["since"]).total_seconds()
                if still_moving:
                    _LOGGER.debug(
                        "%s: still inside %s bbox but speed %.1f km/h ≥ %.0f — "
                        "keeping arrival pending",
                        entity_id, pending["city"],
                        speed_kmh, ARRIVAL_CONFIRM_SPEED_KMH,
                    )
                elif dwell < ARRIVAL_CONFIRM_DWELL_SECONDS:
                    _LOGGER.debug(
                        "%s: inside %s bbox at %.1f km/h but only %.0fs since "
                        "detection (< %ds) — keeping arrival pending",
                        entity_id, pending["city"], speed_kmh or 0,
                        dwell, ARRIVAL_CONFIRM_DWELL_SECONDS,
                    )
                else:
                    p = self._pending_arrival.pop(entity_id)
                    _LOGGER.debug(
                        "%s: confirmed arrival in %s (speed %.1f km/h, "
                        "pending for %.0fs)",
                        entity_id, p["city"], speed_kmh or 0, dwell,
                    )
                    confirmed = self._confirmed_city.get(entity_id)
                    if confirmed is not None and confirmed != p["city"]:
                        self._previous_city[entity_id] = confirmed
                    self._confirmed_city[entity_id] = p["city"]
                    self._departure_announced.discard(entity_id)
                    cached["previous_city"] = self._previous_city.get(entity_id)
                    self._set_place(entity_id, cached, event_title, zone_name, hub)
                    self._push(entity_id, cached)
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
        self._set_place(entity_id, moving, event_title, zone_name, hub)
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
        # Compare against the last *confirmed* city, not the cache: the cache
        # is wiped to city=None while 'moving', so a same-place re-geocode
        # after a GPS-drift excursion would otherwise look like a brand-new
        # arrival.  (Any pending arrival was already discarded on bbox exit,
        # so _pending_arrival is always empty at this point.)
        confirmed: str | None = self._confirmed_city.get(entity_id)
        old_country: str | None = cached.get("country") if cached else None

        entry: dict[str, Any] = {
            "city": new_city,
            "boundingbox": _cap_bbox(geo["boundingbox"], lat, lon),
            "osm_id": geo["osm_id"],
            "place_type": geo["place_type"],
            "latitude": lat,
            "longitude": lon,
            "state": new_city,
            "previous_city": self._previous_city.get(entity_id),
            "country": new_country,
            "country_code": new_country_code,
            "previous_country": old_country,
            ATTR_SPEED: speed_kmh,
            ATTR_SPEED_MPH: speed_mph,
            ATTR_BEARING: bearing,
            ATTR_DIRECTION: direction,
            ATTR_CALENDAR_EVENT: event_title,
        }
        self._set_place(entity_id, entry, event_title, zone_name, hub)
        self._cache[entity_id] = entry
        self._push(entity_id, entry)

        # ── City events ───────────────────────────────────────────────────
        if confirmed is None:
            # First detection at startup — fire immediately, no confirmation needed.
            self._confirmed_city[entity_id] = new_city
            self._fire_arrived(entity_id, new_city, None, new_country, new_country_code, lat, lon)
        elif new_city != confirmed:
            # Departed fires once per departure — person has definitely left.
            # Later drive-through geocodes must not repeat it.
            if entity_id not in self._departure_announced:
                self._departure_announced.add(entity_id)
                self._fire_departed(entity_id, confirmed, old_country, lat, lon)
            # Arrived is held as pending until a later GPS update confirms the
            # person is still inside this bbox, slow, and has dwelt long
            # enough (filters out drive-throughs and stop-start traffic).
            self._pending_arrival[entity_id] = {
                "city": new_city,
                "old_city": confirmed,
                "country": new_country,
                "country_code": new_country_code,
                "since": now,
            }
            _LOGGER.debug(
                "%s: pending arrival in %s (waiting for bbox confirmation)", entity_id, new_city
            )
        elif entity_id in self._departure_announced:
            # Back in the city whose departure was announced (round trip that
            # never confirmed anywhere else) — announce the return, with the
            # same confirmation rules as any other arrival.
            self._pending_arrival[entity_id] = {
                "city": new_city,
                "old_city": self._previous_city.get(entity_id),
                "country": new_country,
                "country_code": new_country_code,
                "since": now,
            }
            _LOGGER.debug(
                "%s: pending return to %s (waiting for bbox confirmation)", entity_id, new_city
            )
        # else: same city re-geocode after GPS drift — nothing to announce.

        # ── Country events ────────────────────────────────────────────────
        if old_country is not None and old_country != new_country:
            self._fire_country_departed(entity_id, old_country, lat, lon)
            self._fire_country_arrived(entity_id, new_country, new_country_code, old_country, lat, lon)
        elif old_country is None and new_country is not None:
            self._fire_country_arrived(entity_id, new_country, new_country_code, None, lat, lon)

        # ── Hub enrichment ────────────────────────────────────────────────
        # Deliberately last: the city has already been published and its
        # events already fired, so however slow or dead Overpass is, none of
        # that is held up waiting for it.
        await self._enrich_with_hubs(
            entity_id, entry, geo["osm_id"], lat, lon, speed_kmh, now,
            event_title, zone_name,
        )

    async def async_handle_zone_update(
        self, entity_id: str, person_state: str | None
    ) -> None:
        """Update the zone/place layer when a person changes zone with no new fix.

        Phone trackers routinely stop publishing GPS once they settle on Wi-Fi
        at home, so the zone transition that matters most arrives as a state
        change carrying no coordinates at all.  This path re-resolves the zone
        against the last known position and refreshes `place` without touching
        the geocoded city, the bbox cache, or Nominatim.
        """
        cached = self._cache.get(entity_id)
        if cached is None:
            cached = _unknown_state()

        lat = cached.get("latitude")
        lon = cached.get("longitude")

        zone_name = resolve_zone(self.hass, person_state, lat, lon)
        if zone_name == self._zone.get(entity_id):
            return

        self._update_zone(entity_id, zone_name, lat, lon)
        self._set_place(
            entity_id,
            cached,
            cached.get(ATTR_CALENDAR_EVENT),
            zone_name,
            self._hub.get(entity_id),
        )
        self._cache[entity_id] = cached
        self._push(entity_id, cached)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_zone(
        self,
        entity_id: str,
        zone_name: str | None,
        lat: float | None,
        lon: float | None,
    ) -> None:
        """Record the current zone and fire arrival/departure on a change.

        No dwell or speed confirmation is applied: HA has already decided the
        person is inside the zone (accounting for GPS accuracy), so unlike a
        geocoded city there is nothing here to second-guess.
        """
        previous = self._zone.get(entity_id)
        if zone_name == previous:
            return

        self._zone[entity_id] = zone_name

        if previous:
            self._previous_zone[entity_id] = previous
            self._fire_zone_departed(entity_id, previous, lat, lon)

        if zone_name:
            self._fire_zone_arrived(
                entity_id, zone_name, self._previous_zone.get(entity_id), lat, lon
            )

    async def _enrich_with_hubs(
        self,
        entity_id: str,
        entry: dict[str, Any],
        osm_id: int | None,
        lat: float,
        lon: float,
        speed_kmh: float | None,
        now: datetime,
        event_title: str | None,
        zone_name: str | None,
    ) -> None:
        """Load hubs for the new city and re-publish only if that changed anything.

        Called after the city entry has already been pushed.  A hub lookup
        that is slow, rate-limited or dead therefore costs nothing except the
        hub name itself.
        """
        if not self._track_hubs:
            return

        await self._refresh_hubs(entity_id, lat, lon, osm_id)
        hub = self._update_hub(entity_id, lat, lon, speed_kmh, now)

        current = entry.get(ATTR_HUB)
        if (hub.name if hub else None) == current:
            return

        self._set_place(entity_id, entry, event_title, zone_name, hub)
        self._push(entity_id, entry)

    async def _refresh_hubs(
        self, entity_id: str, lat: float, lon: float, osm_id: int | None
    ) -> None:
        """Load the hub list for the city just geocoded, if not already cached.

        Called at most once per city change.  Overpass is never queried on a
        plain GPS update — the proximity test runs against this cached list.
        """
        if not self._track_hubs:
            return

        key: int | str = osm_id if osm_id is not None else f"{lat:.2f},{lon:.2f}"

        cached = self._hub_cache.get(key)
        if cached is not None:
            self._active_hubs[entity_id] = cached
            return

        hubs = await self._hub_provider.fetch_hubs(lat, lon, HUB_SEARCH_RADIUS_M)
        if hubs is None:
            # Overpass unavailable.  Keep the previous list rather than
            # blanking it: stale hubs beat no hubs, and the city is unaffected.
            _LOGGER.debug(
                "%s: hub lookup unavailable near (%.4f, %.4f) — keeping city only",
                entity_id, lat, lon,
            )
            return

        self._hub_cache[key] = hubs
        self._active_hubs[entity_id] = hubs

        while len(self._hub_cache) > HUB_CACHE_MAX_CITIES:
            self._hub_cache.pop(next(iter(self._hub_cache)))

    def _update_hub(
        self,
        entity_id: str,
        lat: float,
        lon: float,
        speed_kmh: float | None,
        now: datetime,
    ) -> TransitHub | None:
        """Return the person's confirmed hub, firing arrival/departure events.

        Arrival is held until the person has been inside the hub for
        HUB_CONFIRM_DWELL_SECONDS and is below HUB_CONFIRM_SPEED_KMH.  That
        dwell window is what distinguishes a hub from a station the train
        merely passes through — no OSM tag reliably does it.
        """
        if not self._track_hubs:
            return None

        hubs = self._active_hubs.get(entity_id)
        if not hubs:
            # No hub data for this area (never fetched, or Overpass was down).
            # Keep whatever is confirmed rather than inventing a departure.
            return self._hub.get(entity_id)

        candidate = find_hub(hubs, lat, lon, HUB_NODE_RADIUS_M)
        confirmed = self._hub.get(entity_id)

        # ── Left the confirmed hub ────────────────────────────────────
        if confirmed is not None and (
            candidate is None or candidate.name != confirmed.name
        ):
            self._hub[entity_id] = None
            self._previous_hub[entity_id] = confirmed.name
            self._pending_hub.pop(entity_id, None)
            self._fire_hub_departed(entity_id, confirmed, lat, lon)
            confirmed = None

        if candidate is None:
            self._pending_hub.pop(entity_id, None)
            return None

        if confirmed is not None and candidate.name == confirmed.name:
            return confirmed

        # ── New candidate: hold pending until dwell + speed confirm it ─
        pending = self._pending_hub.get(entity_id)
        if pending is None or pending["hub"].name != candidate.name:
            self._pending_hub[entity_id] = {"hub": candidate, "since": now}
            _LOGGER.debug(
                "%s: pending hub arrival at %s (awaiting %ds dwell)",
                entity_id, candidate.name, HUB_CONFIRM_DWELL_SECONDS,
            )
            return None

        dwell = (now - pending["since"]).total_seconds()
        still_moving = (
            speed_kmh is not None and speed_kmh >= HUB_CONFIRM_SPEED_KMH
        )
        if still_moving or dwell < HUB_CONFIRM_DWELL_SECONDS:
            _LOGGER.debug(
                "%s: at %s for %.0fs at %.1f km/h — still pending",
                entity_id, candidate.name, dwell, speed_kmh or 0,
            )
            return None

        self._pending_hub.pop(entity_id, None)
        self._hub[entity_id] = candidate
        self._fire_hub_arrived(
            entity_id, candidate, self._previous_hub.get(entity_id), lat, lon
        )
        return candidate

    def _set_place(
        self,
        entity_id: str,
        entry: dict[str, Any],
        event_title: str | None,
        zone_name: str | None,
        hub: TransitHub | None = None,
    ) -> None:
        """Resolve `place` / `place_source` for one entry, highest source wins.

        Precedence: calendar event → zone → transit hub → city → moving.
        `city` and `previous_city` are deliberately left alone — the place
        layer sits on top of the geocoded city, it never rewrites it.

        A zone outranks a hub because a zone is a name the user chose
        themselves: someone who has drawn a zone around their local station
        wants to see that name, not the OSM one.
        """
        entry[ATTR_ZONE] = zone_name
        entry[ATTR_PREVIOUS_ZONE] = self._previous_zone.get(entity_id)
        entry[ATTR_HUB] = hub.name if hub else None
        entry[ATTR_HUB_TYPE] = hub.hub_type if hub else None
        entry[ATTR_HUB_CODE] = hub.code if hub else None
        entry[ATTR_PREVIOUS_HUB] = self._previous_hub.get(entity_id)

        if event_title:
            place, source = event_title, PLACE_SOURCE_CALENDAR
        elif zone_name:
            place, source = zone_name, PLACE_SOURCE_ZONE
        elif hub is not None:
            place, source = hub.name, PLACE_SOURCE_HUB
        else:
            state = entry.get("state") or STATE_UNKNOWN
            if state == STATE_MOVING:
                place, source = STATE_MOVING, PLACE_SOURCE_MOVING
            elif state == STATE_UNKNOWN:
                place, source = STATE_UNKNOWN, PLACE_SOURCE_UNKNOWN
            else:
                place, source = state, PLACE_SOURCE_CITY

        entry[ATTR_PLACE] = place
        entry[ATTR_PLACE_SOURCE] = source

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

    def _fire_zone_arrived(
        self,
        entity_id: str,
        zone_name: str,
        previous_zone: str | None,
        lat: float | None,
        lon: float | None,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_ZONE_ARRIVED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_ZONE: zone_name,
                ATTR_PREVIOUS_ZONE: previous_zone,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s arrived at zone %r", EVENT_ZONE_ARRIVED, entity_id, zone_name)

    def _fire_zone_departed(
        self,
        entity_id: str,
        zone_name: str,
        lat: float | None,
        lon: float | None,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_ZONE_DEPARTED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_ZONE: zone_name,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s left zone %r", EVENT_ZONE_DEPARTED, entity_id, zone_name)

    def _fire_hub_arrived(
        self,
        entity_id: str,
        hub: TransitHub,
        previous_hub: str | None,
        lat: float,
        lon: float,
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_HUB_ARRIVED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_HUB: hub.name,
                ATTR_HUB_TYPE: hub.hub_type,
                ATTR_HUB_CODE: hub.code,
                ATTR_PREVIOUS_HUB: previous_hub,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug(
            "%s: %s arrived at %s %r", EVENT_HUB_ARRIVED, entity_id, hub.hub_type, hub.name
        )

    def _fire_hub_departed(
        self, entity_id: str, hub: TransitHub, lat: float, lon: float
    ) -> None:
        self.hass.bus.async_fire(
            EVENT_HUB_DEPARTED,
            {
                ATTR_PERSON_ENTITY_ID: entity_id,
                ATTR_HUB: hub.name,
                ATTR_HUB_TYPE: hub.hub_type,
                ATTR_HUB_CODE: hub.code,
                ATTR_LATITUDE: lat,
                ATTR_LONGITUDE: lon,
            },
        )
        _LOGGER.debug("%s: %s left %r", EVENT_HUB_DEPARTED, entity_id, hub.name)

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
    """Clamp a Nominatim bounding box to sensible min/max sizes.

    Upper cap (MAX_BBOX_DEGREES): prevents getting stuck inside large
    administrative areas like Greater London or Cotswold District.

    Lower floor (MIN_BBOX_DEGREES): prevents rapid moving↔village oscillation
    when Nominatim returns a tiny hamlet bbox and indoor GPS drift (10–100 m)
    constantly pushes the person outside it.
    """
    if boundingbox is None:
        return None
    try:
        min_lat, max_lat, min_lon, max_lon = (float(v) for v in boundingbox)
    except (TypeError, ValueError):
        return None

    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon

    # ── Containment guard ─────────────────────────────────────────────────
    # Reverse geocoding can snap to a place node kilometres away whose bbox
    # doesn't contain the person at all.  Caching it would make every
    # subsequent GPS fix look like "left the city" → endless re-arrivals.
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        _LOGGER.debug(
            "Bbox [%f..%f, %f..%f] does not contain (%.5f, %.5f) — "
            "replacing with %.3f° box centred on the person",
            min_lat, max_lat, min_lon, max_lon, lat, lon, MIN_BBOX_DEGREES,
        )
        return [
            str(lat - MIN_BBOX_DEGREES),
            str(lat + MIN_BBOX_DEGREES),
            str(lon - MIN_BBOX_DEGREES),
            str(lon + MIN_BBOX_DEGREES),
        ]

    # ── Upper cap ─────────────────────────────────────────────────────────
    if lat_span > MAX_BBOX_DEGREES * 2 or lon_span > MAX_BBOX_DEGREES * 2:
        _LOGGER.debug(
            "Capping oversized bbox (%.3f°×%.3f°) to %.2f° centred on (%.5f, %.5f)",
            lat_span, lon_span, MAX_BBOX_DEGREES, lat, lon,
        )
        return [
            str(lat - MAX_BBOX_DEGREES),
            str(lat + MAX_BBOX_DEGREES),
            str(lon - MAX_BBOX_DEGREES),
            str(lon + MAX_BBOX_DEGREES),
        ]

    # ── Lower floor ───────────────────────────────────────────────────────
    if lat_span < MIN_BBOX_DEGREES * 2 or lon_span < MIN_BBOX_DEGREES * 2:
        _LOGGER.debug(
            "Expanding undersized bbox (%.4f°×%.4f°) to %.3f° centred on (%.5f, %.5f)",
            lat_span, lon_span, MIN_BBOX_DEGREES, lat, lon,
        )
        return [
            str(lat - MIN_BBOX_DEGREES),
            str(lat + MIN_BBOX_DEGREES),
            str(lon - MIN_BBOX_DEGREES),
            str(lon + MIN_BBOX_DEGREES),
        ]

    return boundingbox


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
        ATTR_ZONE: None,
        ATTR_PREVIOUS_ZONE: None,
        ATTR_HUB: None,
        ATTR_HUB_TYPE: None,
        ATTR_HUB_CODE: None,
        ATTR_PREVIOUS_HUB: None,
        ATTR_PLACE: STATE_UNKNOWN,
        ATTR_PLACE_SOURCE: PLACE_SOURCE_UNKNOWN,
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
        # "or previous_city" keeps the value alive across chained moving
        # states (a moving cache entry has city=None).
        "previous_city": (cached.get("city") or cached.get("previous_city")) if cached else None,
        # Preserve last known country while in transit — useful for templates.
        "country": cached.get("country") if cached else None,
        "country_code": cached.get("country_code") if cached else None,
        "previous_country": cached.get("previous_country") if cached else None,
        ATTR_SPEED: speed_kmh,
        ATTR_SPEED_MPH: speed_mph,
        ATTR_BEARING: bearing,
        ATTR_DIRECTION: direction,
        ATTR_CALENDAR_EVENT: event_title,
        ATTR_ZONE: None,
        ATTR_PREVIOUS_ZONE: None,
        ATTR_HUB: None,
        ATTR_HUB_TYPE: None,
        ATTR_HUB_CODE: None,
        ATTR_PREVIOUS_HUB: None,
        ATTR_PLACE: STATE_MOVING,
        ATTR_PLACE_SOURCE: PLACE_SOURCE_MOVING,
    }
