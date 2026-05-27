"""Device triggers for Whereabouts — exposed in the HA automation wizard."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_PERSON_ENTITY_ID,
    DOMAIN,
    EVENT_CALENDAR_ARRIVED,
    EVENT_CALENDAR_DEPARTED,
    EVENT_CITY_ARRIVED,
    EVENT_COUNTRY_ARRIVED,
    EVENT_COUNTRY_DEPARTED,
    EVENT_STARTED_MOVING,
)

_LOGGER = logging.getLogger(__name__)

_TRIGGER_EVENT: dict[str, str] = {
    "city_changed":       EVENT_CITY_ARRIVED,
    "started_moving":     EVENT_STARTED_MOVING,
    "country_arrived":    EVENT_COUNTRY_ARRIVED,
    "country_departed":   EVENT_COUNTRY_DEPARTED,
    "calendar_arrived":   EVENT_CALENDAR_ARRIVED,
    "calendar_departed":  EVENT_CALENDAR_DEPARTED,
}

# Minimal schema — no DEVICE_TRIGGER_BASE_SCHEMA import needed
TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PLATFORM): "device",
        vol.Required(CONF_DOMAIN): DOMAIN,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_TYPE): vol.In(_TRIGGER_EVENT),
    },
    extra=vol.ALLOW_EXTRA,
)


def _person_entity_id(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the person entity_id stored in the Whereabouts device identifiers."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for id_domain, identifier in device.identifiers:
        if id_domain == DOMAIN:
            return identifier
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Return all triggers available for a Whereabouts device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return []
    # Only expose triggers for devices registered by this integration
    if not any(domain == DOMAIN for domain, _ in device.identifiers):
        return []

    _LOGGER.debug("async_get_triggers called for device %s", device_id)

    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in _TRIGGER_EVENT
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: Any,
    trigger_info: Any,
) -> CALLBACK_TYPE:
    """Listen for the matching HA bus event and call action when it fires."""
    person_id = _person_entity_id(hass, config[CONF_DEVICE_ID])
    event_type = _TRIGGER_EVENT[config[CONF_TYPE]]

    _LOGGER.debug(
        "Attaching trigger %s for person %s (event: %s)",
        config[CONF_TYPE], person_id, event_type,
    )

    @callback
    def _handle_event(event: Any) -> None:
        # Filter to the specific person this device represents
        if person_id and event.data.get(ATTR_PERSON_ENTITY_ID) != person_id:
            return
        hass.async_create_task(
            action(
                {
                    "trigger": {
                        CONF_PLATFORM: "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                        CONF_TYPE: config[CONF_TYPE],
                        "event": event,
                    }
                }
            )
        )

    return hass.bus.async_listen(event_type, _handle_event)
