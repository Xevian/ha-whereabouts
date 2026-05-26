"""Sensor platform for Whereabouts."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_BEARING,
    ATTR_CALENDAR_EVENT,
    ATTR_COUNTRY,
    ATTR_COUNTRY_CODE,
    ATTR_DIRECTION,
    ATTR_OSM_ID,
    ATTR_PLACE_TYPE,
    ATTR_PREVIOUS_CITY,
    ATTR_PREVIOUS_COUNTRY,
    ATTR_SPEED,
    ATTR_SPEED_MPH,
    DOMAIN,
    STATE_MOVING,
    STATE_UNKNOWN,
)
from .coordinator import WhereaboutsCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Whereabouts sensor entities from a config entry."""
    coordinator: WhereaboutsCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        WhereaboutsSensor(coordinator, person_entity_id)
        for person_entity_id in coordinator._person_entity_ids
    )


class WhereaboutsSensor(CoordinatorEntity[WhereaboutsCoordinator], SensorEntity):
    """Sensor reporting the current city for one tracked person.

    State:      city name | event title | "moving" | "unknown"
    Attributes: place_type, osm_id, country, country_code,
                previous_city, previous_country, speed_kmh, speed_mph,
                bearing, direction, calendar_event
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:map-marker-account"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: WhereaboutsCoordinator,
        person_entity_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._person_entity_id = person_entity_id

        # Stable unique_id — dot replaced to satisfy HA entity registry rules.
        safe_id = person_entity_id.replace(".", "_")
        self._attr_unique_id = f"whereabouts_{safe_id}"

        person_name = (
            person_entity_id.split(".")[-1].replace("_", " ").title()
        )
        self._attr_name = f"Whereabouts {person_name}"

    # ------------------------------------------------------------------
    # Device info — groups sensor + event tracker under one device per person
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
    # State & attributes
    # ------------------------------------------------------------------

    @property
    def native_value(self) -> str:
        """Return calendar event title (if at one), city name, 'moving', or 'unknown'."""
        data = self.coordinator.data
        if not data or self._person_entity_id not in data:
            return STATE_UNKNOWN
        entry = data[self._person_entity_id]
        # Calendar event takes display priority over city name.
        return entry.get(ATTR_CALENDAR_EVENT) or entry.get("state", STATE_UNKNOWN)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose place, country, motion, and history attributes.

        Latitude and longitude are intentionally omitted — exposing them as
        state attributes causes the map card to plot a duplicate pin on top of
        the person entity's existing pin.
        """
        data = self.coordinator.data
        if not data or self._person_entity_id not in data:
            return {}

        entry = data[self._person_entity_id]
        attrs: dict[str, Any] = {}

        # Present only when not None (avoids cluttering attributes panel).
        for key, attr in (
            ("place_type", ATTR_PLACE_TYPE),
            ("osm_id", ATTR_OSM_ID),
            ("country", ATTR_COUNTRY),
            ("country_code", ATTR_COUNTRY_CODE),
            (ATTR_SPEED, ATTR_SPEED),
            (ATTR_SPEED_MPH, ATTR_SPEED_MPH),
            (ATTR_BEARING, ATTR_BEARING),
            (ATTR_DIRECTION, ATTR_DIRECTION),
            (ATTR_CALENDAR_EVENT, ATTR_CALENDAR_EVENT),
        ):
            if entry.get(key) is not None:
                attrs[attr] = entry[key]

        # Always include previous_* (may be None) so templates can rely on them.
        attrs[ATTR_PREVIOUS_CITY] = entry.get("previous_city")
        attrs[ATTR_PREVIOUS_COUNTRY] = entry.get("previous_country")

        return attrs

    @property
    def available(self) -> bool:
        """Unavailable only when the coordinator has never succeeded."""
        return self.coordinator.last_update_success
