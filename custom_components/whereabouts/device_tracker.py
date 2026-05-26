"""Device tracker platform — next calendar event with a location on the map."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EVENT_RADIUS_M,
    CONF_PERSON_CALENDARS,
    DEFAULT_EVENT_RADIUS_M,
    DOMAIN,
)
from .coordinator import WhereaboutsCoordinator

_LOGGER = logging.getLogger(__name__)

# How far ahead to search for events with a location.
_EVENT_LOOKAHEAD_DAYS = 90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one event-tracker entity per person that has a linked calendar."""
    coordinator: WhereaboutsCoordinator = hass.data[DOMAIN][entry.entry_id]
    person_calendars: dict[str, str | None] = entry.data.get(CONF_PERSON_CALENDARS, {})
    event_radius_m: float = entry.data.get(CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M)

    async_add_entities(
        WhereaboutsEventTracker(coordinator, person_id, calendar_id, event_radius_m)
        for person_id, calendar_id in person_calendars.items()
        if calendar_id
    )


class WhereaboutsEventTracker(CoordinatorEntity[WhereaboutsCoordinator], TrackerEntity):
    """Map pin for the next calendar event that has a location.

    Uses calendar.get_events to scan up to _EVENT_LOOKAHEAD_DAYS ahead,
    skipping events without a location, so the pin always points at
    something meaningful rather than going blank between events.

    State:         event title | None (shown as "unknown")
    Map pin:       geocoded event venue
    Radius circle: location_accuracy = event_radius_m
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-map"
    _attr_should_poll = False

    # Purple map-pin icon served from the bundled icons directory.
    # Registered in __init__.py via hass.http.async_register_static_paths.
    _attr_entity_picture = "/whereabouts/icons/event_pin.svg"

    def __init__(
        self,
        coordinator: WhereaboutsCoordinator,
        person_entity_id: str,
        calendar_entity_id: str,
        event_radius_m: float,
    ) -> None:
        super().__init__(coordinator)
        self._person_entity_id = person_entity_id
        self._calendar_entity_id = calendar_entity_id
        self._event_radius_m = event_radius_m

        # Cached result of the last successful event scan.
        self._next_title: str | None = None
        self._next_location: str | None = None
        self._next_start: str | None = None
        self._next_end: str | None = None

        safe_id = person_entity_id.replace(".", "_")
        self._attr_unique_id = f"whereabouts_event_{safe_id}"
        person_name = person_entity_id.split(".")[-1].replace("_", " ").title()
        self._attr_name = f"Next Event {person_name}"

    # ------------------------------------------------------------------
    # Device info — same identifiers as the sensor so both share one device
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        person_name = self._person_entity_id.split(".")[-1].replace("_", " ").title()
        return DeviceInfo(
            identifiers={(DOMAIN, self._person_entity_id)},
            name=f"Whereabouts {person_name}",
            manufacturer="Whereabouts",
            model="Location Tracker",
            entry_type=DeviceEntryType.SERVICE,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Scan for the first upcoming event with a location and subscribe
        to calendar changes so the pin refreshes automatically."""
        await super().async_added_to_hass()

        # Refresh whenever the calendar entity state changes (new event active,
        # event ends, calendar resyncs from Google, etc.).
        @callback
        def _on_calendar_changed(event: Any) -> None:
            self.hass.async_create_task(self._async_scan_and_refresh())

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._calendar_entity_id], _on_calendar_changed
            )
        )

        # Initial scan — runs immediately so the pin is visible on first load.
        await self._async_scan_and_refresh()

    # ------------------------------------------------------------------
    # TrackerEntity interface
    # ------------------------------------------------------------------

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        c = self._cached_coords()
        return c[0] if c else None

    @property
    def longitude(self) -> float | None:
        c = self._cached_coords()
        return c[1] if c else None

    @property
    def location_accuracy(self) -> int:
        """Arrival radius in metres — rendered as a circle on the HA map."""
        return int(self._event_radius_m)

    @property
    def location_name(self) -> str | None:
        """Event title as entity state (overrides HA zone detection)."""
        c = self._cached_coords()
        return c[2] if c else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "calendar_entity": self._calendar_entity_id,
        }
        if self._next_title:
            attrs["event_title"] = self._next_title
        if self._next_location:
            attrs["event_location"] = self._next_location
        if self._next_start:
            attrs["event_start"] = self._next_start
        if self._next_end:
            attrs["event_end"] = self._next_end
        c = self._cached_coords()
        if c:
            attrs["geocoded_lat"] = c[0]
            attrs["geocoded_lon"] = c[1]
        elif self._next_location:
            attrs["debug"] = f"Location {self._next_location!r} not yet geocoded"
        else:
            attrs["debug"] = "No upcoming events with a location found"
        return attrs

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cached_coords(self) -> tuple[float, float, str] | None:
        """Return (lat, lon, title) from cache, or None."""
        if not self._next_location or not self._next_title:
            return None
        coords = self.coordinator._event_location_cache.get(self._next_location)
        if coords is None:
            return None
        return coords[0], coords[1], self._next_title

    async def _async_scan_and_refresh(self) -> None:
        """Call calendar.get_events, find the next event with a location,
        geocode it if needed, then redraw the map pin."""
        title, location, start, end = await self._async_find_next_located_event()

        self._next_title = title
        self._next_location = location
        self._next_start = start
        self._next_end = end

        if location and location not in self.coordinator._event_location_cache:
            coords = await self.coordinator._geocoder.geocode_address(location)
            if coords:
                self.coordinator._event_location_cache[location] = coords
                _LOGGER.debug(
                    "Geocoded event location %r → %s", location, coords
                )
            else:
                _LOGGER.warning(
                    "Could not geocode event location %r for %s",
                    location,
                    self._person_entity_id,
                )

        self.async_write_ha_state()

    async def _async_find_next_located_event(
        self,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Return (title, location, start, end) of the next event that has a
        non-empty location, or (None, None, None, None) if none found.

        Uses calendar.get_events (HA 2023.3+) to scan ahead up to
        _EVENT_LOOKAHEAD_DAYS days, skipping events without a location.
        Falls back to the current calendar entity attributes on older HA.
        """
        now = dt_util.now()
        end_dt = now + timedelta(days=_EVENT_LOOKAHEAD_DAYS)

        try:
            response: dict = await self.hass.services.async_call(
                "calendar",
                "get_events",
                service_data={
                    "entity_id": self._calendar_entity_id,
                    "start_date_time": now.isoformat(),
                    "end_date_time": end_dt.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "calendar.get_events not available (%s) — falling back to "
                "calendar entity attributes",
                err,
            )
            return self._fallback_from_state()

        events: list[dict] = (
            (response or {})
            .get(self._calendar_entity_id, {})
            .get("events", [])
        )

        for event in events:
            location = (event.get("location") or "").strip()
            if not location:
                continue  # skip events without a location
            title = event.get("summary") or "Event"
            start = event.get("start")
            end = event.get("end")
            _LOGGER.debug(
                "%s: next event with location — %r at %r (starts %s)",
                self._person_entity_id,
                title,
                location,
                start,
            )
            return title, location, start, end

        _LOGGER.debug(
            "%s: no events with a location in the next %d days",
            self._person_entity_id,
            _EVENT_LOOKAHEAD_DAYS,
        )
        return None, None, None, None

    def _fallback_from_state(
        self,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Fallback for HA < 2023.3: read the single event from entity state."""
        cal = self.hass.states.get(self._calendar_entity_id)
        if cal is None:
            return None, None, None, None
        location = (cal.attributes.get("location") or "").strip() or None
        title = cal.attributes.get("message")
        start = cal.attributes.get("start_time")
        end = cal.attributes.get("end_time")
        if location and title:
            return title, location, start, end
        return None, None, None, None
