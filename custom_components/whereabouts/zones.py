"""Home Assistant zone resolution for Whereabouts.

A zone is a place the user has already named — "Home", "Work", "Gran's
House" — so when someone is inside one, that name beats anything Nominatim
can return for the same coordinates.

Resolution is deliberately cheap: for active zones HA has *already* done the
geometry and written the zone's friendly name into the person entity's state,
so there is nothing to compute.  Only passive zones (which HA excludes from
person state by design) need a distance check, and only when the person is
otherwise not_home.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.location import distance as location_distance

from .const import (
    NON_ZONE_PERSON_STATES,
    PERSON_STATE_HOME,
)

_LOGGER = logging.getLogger(__name__)

_HOME_ZONE_ENTITY = "zone.home"
_DEFAULT_HOME_NAME = "Home"


def resolve_zone(
    hass: HomeAssistant,
    person_state: str | None,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    """Return the display name of the zone the person is in, or None.

    person_state is the raw state of the person entity.  HA sets it to the
    zone's friendly name for any non-passive zone, to the literal "home" for
    the home zone, and to "not_home" when outside every zone.
    """
    if person_state:
        normalised = person_state.strip()
        if normalised.lower() == PERSON_STATE_HOME:
            # HA uses the literal "home", not the zone's friendly name, so the
            # home zone is the one case that needs looking up — the user may
            # well have renamed it.
            return _home_zone_name(hass)
        if normalised.lower() not in NON_ZONE_PERSON_STATES:
            # Any other value is already the zone's friendly name.
            return normalised

    # not_home (or no state at all): HA never reports passive zones in person
    # state, so they are the only thing left that could still be a match.
    if lat is not None and lon is not None:
        return _passive_zone_at(hass, lat, lon)

    return None


def _home_zone_name(hass: HomeAssistant) -> str:
    """Return the friendly name of zone.home, falling back to "Home"."""
    state = hass.states.get(_HOME_ZONE_ENTITY)
    if state is None:
        return _DEFAULT_HOME_NAME
    return state.attributes.get("friendly_name") or _DEFAULT_HOME_NAME


def _passive_zone_at(hass: HomeAssistant, lat: float, lon: float) -> str | None:
    """Return the name of the smallest passive zone containing (lat, lon).

    Smallest wins so that a passive zone nested inside a larger one (a
    building within a campus) reports the more specific name.
    """
    best_name: str | None = None
    best_radius = float("inf")

    for state in hass.states.async_all("zone"):
        attrs: dict[str, Any] = state.attributes
        if not attrs.get("passive"):
            continue

        zone_lat = attrs.get("latitude")
        zone_lon = attrs.get("longitude")
        radius = attrs.get("radius")
        if zone_lat is None or zone_lon is None or radius is None:
            continue

        try:
            radius_m = float(radius)
            distance = location_distance(lat, lon, float(zone_lat), float(zone_lon))
        except (TypeError, ValueError):
            continue

        if distance is None:
            continue

        if distance <= radius_m and radius_m < best_radius:
            best_radius = radius_m
            best_name = attrs.get("friendly_name") or state.name

    if best_name:
        _LOGGER.debug(
            "(%.6f, %.6f) is inside passive zone %r (radius %.0fm)",
            lat, lon, best_name, best_radius,
        )
    return best_name
