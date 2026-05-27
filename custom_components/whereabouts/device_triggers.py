"""Device triggers for Whereabouts — exposed in the HA automation wizard."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
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

# All triggers are event-based — avoids state-trigger import fragility
# and works reliably across all supported HA versions.
_TRIGGER_EVENT: dict[str, str] = {
    "city_changed":       EVENT_CITY_ARRIVED,
    "started_moving":     EVENT_STARTED_MOVING,
    "country_arrived":    EVENT_COUNTRY_ARRIVED,
    "country_departed":   EVENT_COUNTRY_DEPARTED,
    "calendar_arrived":   EVENT_CALENDAR_ARRIVED,
    "calendar_departed":  EVENT_CALENDAR_DEPARTED,
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(_TRIGGER_EVENT)}
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
    if _person_entity_id(hass, device_id) is None:
        return []
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
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach an event trigger filtered to the specific person."""
    person_id = _person_entity_id(hass, config[CONF_DEVICE_ID])
    event_type = _TRIGGER_EVENT[config[CONF_TYPE]]

    return await event_trigger.async_attach_trigger(
        hass,
        event_trigger.TRIGGER_SCHEMA(
            {
                CONF_PLATFORM: "event",
                event_trigger.CONF_EVENT_TYPE: event_type,
                event_trigger.CONF_EVENT_DATA: {
                    ATTR_PERSON_ENTITY_ID: person_id,
                },
            }
        ),
        action,
        trigger_info,
        platform_type="device",
    )
