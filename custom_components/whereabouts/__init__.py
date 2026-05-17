"""Whereabouts integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_EVENT_RADIUS_M,
    CONF_PERSON_CALENDARS,
    CONF_PERSONS,
    CONF_SCAN_INTERVAL,
    DEFAULT_EVENT_RADIUS_M,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import WhereaboutsCoordinator

_LOGGER = logging.getLogger(__name__)

# URL path under which bundled static assets (icons, etc.) are served.
_STATIC_URL = f"/{DOMAIN}/icons"
_STATIC_DIR = Path(__file__).parent / "icons"
_STATIC_REGISTERED_KEY = f"{DOMAIN}_static_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Whereabouts from a config entry."""

    # Register the bundled icons directory once per HA instance so the
    # device-tracker entity_picture URL resolves correctly on the map.
    if not hass.data.get(_STATIC_REGISTERED_KEY):
        try:
            # HA 2024.x+
            from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415
            await hass.http.async_register_static_paths(
                [StaticPathConfig(_STATIC_URL, str(_STATIC_DIR), True)]
            )
        except (AttributeError, ImportError):
            # HA 2023.x fallback
            hass.http.register_static_path(_STATIC_URL, str(_STATIC_DIR), True)
        hass.data[_STATIC_REGISTERED_KEY] = True
        _LOGGER.debug("Registered static path %s → %s", _STATIC_URL, _STATIC_DIR)

    person_entity_ids: list[str] = entry.data[CONF_PERSONS]
    geocode_cooldown_seconds: int = (
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES) * 60
    )

    coordinator = WhereaboutsCoordinator(
        hass,
        person_entity_ids=person_entity_ids,
        geocode_cooldown_seconds=geocode_cooldown_seconds,
        person_calendars=entry.data.get(CONF_PERSON_CALENDARS, {}),
        event_radius_m=entry.data.get(CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M),
    )

    # Seed initial state and geocode any person that already has GPS coords.
    # This runs synchronously before sensors are created so they always have
    # something to display — even if it's STATE_UNKNOWN.
    await coordinator.async_initialize()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── Real-time location listener ───────────────────────────────────────
    # Fires on every state-change of any tracked person entity.
    # The @callback decorator marks it as synchronous; async work is
    # scheduled via async_create_task so we don't block the event bus.

    @callback
    def _person_location_changed(event: Event) -> None:
        entity_id: str = event.data["entity_id"]
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        new_lat = new_state.attributes.get("latitude")
        new_lon = new_state.attributes.get("longitude")

        if new_lat is None or new_lon is None:
            # Tracker lost GPS (e.g. switched to Wi-Fi / home zone).
            return

        # Skip if coordinates haven't actually changed — person entities
        # can fire state_changed for zone transitions without moving.
        if old_state is not None:
            if (
                old_state.attributes.get("latitude") == new_lat
                and old_state.attributes.get("longitude") == new_lon
            ):
                return

        hass.async_create_task(
            coordinator.async_handle_location_update(
                entity_id, float(new_lat), float(new_lon)
            )
        )

    entry.async_on_unload(
        async_track_state_change_event(
            hass, person_entity_ids, _person_location_changed
        )
    )

    # Reload when options change (new persons, different cooldown, or calendar changes).
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry after options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Whereabouts config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
