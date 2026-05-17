"""Config flow for Whereabouts."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_EVENT_RADIUS_M,
    CONF_PERSON_CALENDARS,
    CONF_PERSONS,
    CONF_SCAN_INTERVAL,
    DEFAULT_EVENT_RADIUS_M,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Field name used in the calendar-assignment step.
_CALENDAR_FIELD = "calendar_entity"


def _build_main_schema(
    default_persons: list[str] | None = None,
    default_interval: int = DEFAULT_SCAN_INTERVAL_MINUTES,
    default_radius: int = DEFAULT_EVENT_RADIUS_M,
) -> vol.Schema:
    """Schema for step 1: persons, geocode cooldown, event radius."""
    return vol.Schema(
        {
            vol.Required(
                CONF_PERSONS,
                **({"default": default_persons} if default_persons is not None else {}),
            ): selector.selector(
                {"entity": {"domain": "person", "multiple": True}}
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=default_interval,
            ): selector.selector(
                {
                    "number": {
                        "min": 1,
                        "max": 1440,
                        "unit_of_measurement": "minutes",
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                CONF_EVENT_RADIUS_M,
                default=default_radius,
            ): selector.selector(
                {
                    "number": {
                        "min": 50,
                        "max": 5000,
                        "unit_of_measurement": "m",
                        "mode": "box",
                    }
                }
            ),
        }
    )


def _build_calendar_schema(existing: str | None = None) -> vol.Schema:
    """Schema for one calendar-assignment step (one person at a time)."""
    field: Any = (
        vol.Optional(_CALENDAR_FIELD, default=existing)
        if existing
        else vol.Optional(_CALENDAR_FIELD)
    )
    return vol.Schema(
        {field: selector.selector({"entity": {"domain": "calendar"}})}
    )


class WhereaboutsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup for Whereabouts."""

    VERSION = 1

    def __init__(self) -> None:
        self._persons: list[str] = []
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL_MINUTES
        self._event_radius_m: int = DEFAULT_EVENT_RADIUS_M
        self._person_calendars: dict[str, str | None] = {}
        self._remaining_persons: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 — pick persons, geocode cooldown, and event radius."""
        errors: dict[str, str] = {}

        if user_input is not None:
            persons: list[str] = user_input.get(CONF_PERSONS) or []
            scan_interval: int = user_input.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
            )

            if not persons:
                errors[CONF_PERSONS] = "no_persons_selected"
            elif scan_interval < 1:
                errors[CONF_SCAN_INTERVAL] = "scan_interval_too_low"
            else:
                self._persons = persons
                self._scan_interval = scan_interval
                self._event_radius_m = user_input.get(
                    CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M
                )
                self._remaining_persons = list(persons)
                self._person_calendars = {}
                return await self.async_step_calendars()

        return self.async_show_form(
            step_id="user",
            data_schema=_build_main_schema(),
            errors=errors,
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2+ — assign an optional calendar to each person in turn."""
        # Save the calendar selection for the person we just asked about.
        if user_input is not None and self._remaining_persons:
            person = self._remaining_persons[0]
            self._person_calendars[person] = user_input.get(_CALENDAR_FIELD) or None
            self._remaining_persons = self._remaining_persons[1:]

        # More persons still to configure?
        if self._remaining_persons:
            person = self._remaining_persons[0]
            person_name = _person_display_name(person)
            return self.async_show_form(
                step_id="calendars",
                data_schema=_build_calendar_schema(),
                description_placeholders={"person_name": person_name},
            )

        # All persons done — create the config entry.
        await self.async_set_unique_id("_".join(sorted(self._persons)))
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=_entry_title(self._persons),
            data={
                CONF_PERSONS: self._persons,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_EVENT_RADIUS_M: self._event_radius_m,
                CONF_PERSON_CALENDARS: self._person_calendars,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WhereaboutsOptionsFlow:
        """Return the options flow so users can reconfigure after setup."""
        return WhereaboutsOptionsFlow(config_entry)


class WhereaboutsOptionsFlow(OptionsFlow):
    """Allow changing persons, cooldown, and calendar assignments."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._persons: list[str] = []
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL_MINUTES
        self._event_radius_m: int = DEFAULT_EVENT_RADIUS_M
        self._person_calendars: dict[str, str | None] = {}
        self._remaining_persons: list[str] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show a menu so users can jump straight to what they want to change."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "calendars"],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit persons, geocode cooldown, and event radius."""
        errors: dict[str, str] = {}
        current = self._config_entry.data

        if user_input is not None:
            persons: list[str] = user_input.get(CONF_PERSONS) or []
            scan_interval: int = user_input.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
            )

            if not persons:
                errors[CONF_PERSONS] = "no_persons_selected"
            elif scan_interval < 1:
                errors[CONF_SCAN_INTERVAL] = "scan_interval_too_low"
            else:
                self._persons = persons
                self._scan_interval = scan_interval
                self._event_radius_m = user_input.get(
                    CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M
                )
                self._remaining_persons = list(persons)
                # Carry existing calendar assignments forward; new persons get None.
                existing_calendars: dict[str, str | None] = current.get(
                    CONF_PERSON_CALENDARS, {}
                )
                self._person_calendars = {
                    p: existing_calendars.get(p) for p in persons
                }
                return await self.async_step_calendars()

        return self.async_show_form(
            step_id="settings",
            data_schema=_build_main_schema(
                default_persons=current.get(CONF_PERSONS, []),
                default_interval=current.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                ),
                default_radius=current.get(CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M),
            ),
            errors=errors,
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign a calendar to each person in turn.

        Can be entered directly from the menu (skipping the settings step),
        in which case we initialise state from the current config entry.
        """
        # If coming straight from the menu, load state from config.
        if not self._persons:
            current = self._config_entry.data
            self._persons = current.get(CONF_PERSONS, [])
            self._scan_interval = current.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
            )
            self._event_radius_m = current.get(CONF_EVENT_RADIUS_M, DEFAULT_EVENT_RADIUS_M)
            existing_calendars: dict[str, str | None] = current.get(
                CONF_PERSON_CALENDARS, {}
            )
            self._person_calendars = {
                p: existing_calendars.get(p) for p in self._persons
            }
            self._remaining_persons = list(self._persons)

        if user_input is not None and self._remaining_persons:
            person = self._remaining_persons[0]
            self._person_calendars[person] = user_input.get(_CALENDAR_FIELD) or None
            self._remaining_persons = self._remaining_persons[1:]

        if self._remaining_persons:
            person = self._remaining_persons[0]
            person_name = _person_display_name(person)
            existing = self._person_calendars.get(person)
            return self.async_show_form(
                step_id="calendars",
                data_schema=_build_calendar_schema(existing),
                description_placeholders={"person_name": person_name},
            )

        # All done — write back to config_entry.data.
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data={
                CONF_PERSONS: self._persons,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_EVENT_RADIUS_M: self._event_radius_m,
                CONF_PERSON_CALENDARS: self._person_calendars,
            },
        )
        return self.async_create_entry(title="", data={})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _person_display_name(person_entity_id: str) -> str:
    """Turn 'person.john_doe' into 'John Doe'."""
    return person_entity_id.split(".")[-1].replace("_", " ").title()


def _entry_title(persons: list[str]) -> str:
    """Human-readable config entry title, e.g. 'Whereabouts (John, Jane)'."""
    names = [_person_display_name(p) for p in persons]
    joined = ", ".join(names[:3])
    if len(names) > 3:
        joined += f" +{len(names) - 3}"
    return f"Whereabouts ({joined})"
