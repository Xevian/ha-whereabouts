"""Device triggers for Whereabouts — exposed in the HA automation wizard."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.components.homeassistant.triggers import state as state_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TO,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_PERSON_ENTITY_ID,
    DOMAIN,
    EVENT_CALENDAR_ARRIVED,
    EVENT_CALENDAR_DEPARTED,
    EVENT_COUNTRY_ARRIVED,
    EVENT_COUNTRY_DEPARTED,
    STATE_MOVING,
)

# ── Trigger type identifiers ──────────────────────────────────────────────────

TRIGGER_CITY_CHANGED     = "city_changed"      # sensor state → any new city name
TRIGGER_STARTED_MOVING   = "started_moving"    # sensor state → "moving"
TRIGGER_COUNTRY_ARRIVED  = "country_arrived"
TRIGGER_COUNTRY_DEPARTED = "country_departed"
TRIGGER_CALENDAR_ARRIVED  = "calendar_arrived"
TRIGGER_CALENDAR_DEPARTED = "calendar_departed"

# State-based triggers use the sensor entity; event-based use the HA bus.
_STATE_TRIGGERS: set[str] = {TRIGGER_CITY_CHANGED, TRIGGER_STARTED_MOVING}

_EVENT_TRIGGERS: dict[str, str] = {
    TRIGGER_COUNTRY_ARRIVED:  EVENT_COUNTRY_ARRIVED,
    TRIGGER_COUNTRY_DEPARTED: EVENT_COUNTRY_DEPARTED,
    TRIGGER_CALENDAR_ARRIVED:  EVENT_CALENDAR_ARRIVED,
    TRIGGER_CALENDAR_DEPARTED: EVENT_CALENDAR_DEPARTED,
}

_ALL_TRIGGER_TYPES = list(_STATE_TRIGGERS) + list(_EVENT_TRIGGERS)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(_ALL_TRIGGER_TYPES)}
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _person_entity_id(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the person entity_id stored in the Whereabouts device identifiers."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for id_domain, identifier in device.identifiers:
        if id_domain == DOMAIN:
            return identifier
    return None


def _sensor_entity_id(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the sensor entity_id for this Whereabouts device."""
    for entry in er.async_entries_for_device(er.async_get(hass), device_id):
        if entry.domain == "sensor" and entry.platform == DOMAIN:
            return entry.entity_id
    return None


# ── Public interface ──────────────────────────────────────────────────────────

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
        for trigger_type in _ALL_TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach the underlying HA trigger for the requested type."""
    trigger_type: str = config[CONF_TYPE]

    # ── State-based triggers (sensor entity) ─────────────────────────────────
    if trigger_type in _STATE_TRIGGERS:
        sensor_id = _sensor_entity_id(hass, config[CONF_DEVICE_ID])
        if sensor_id is None:
            return lambda: None

        trigger_data: dict[str, Any] = {
            CONF_PLATFORM: "state",
            CONF_ENTITY_ID: [sensor_id],
        }
        if trigger_type == TRIGGER_STARTED_MOVING:
            # Only fire when the sensor reaches exactly "moving".
            trigger_data[CONF_TO] = STATE_MOVING
        # TRIGGER_CITY_CHANGED has no 'to' filter — fires on every state change,
        # which covers city → city, moving → city, and city → moving transitions.
        # Users can add a condition (e.g. state != "moving") if needed.

        return await state_trigger.async_attach_trigger(
            hass,
            state_trigger.TRIGGER_SCHEMA(trigger_data),
            action,
            trigger_info,
            platform_type="device",
        )

    # ── Event-based triggers (HA bus events) ─────────────────────────────────
    person_id = _person_entity_id(hass, config[CONF_DEVICE_ID])
    event_type = _EVENT_TRIGGERS[trigger_type]

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
