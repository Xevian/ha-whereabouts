"""Nominatim reverse geocoder for City Tracker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import (
    NOMINATIM_SEARCH_URL,
    NOMINATIM_TIMEOUT_SECONDS,
    NOMINATIM_URL,
    NOMINATIM_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Address keys tried in order — first match wins.
# "suburb" sits between city and town: catches named London neighbourhoods
# (Westminster, Hackney…) that would otherwise return "Greater London".
# "municipality" covers some European cities not tagged as city/town.
_CITY_ADDRESS_KEYS = ("city", "suburb", "municipality", "town", "village", "hamlet")

# place_types that warrant a second pass at lower zoom to find the parent town.
_RURAL_PLACE_TYPES = {"village", "hamlet"}


class NominatimGeocoder:
    """Async reverse geocoder backed by Nominatim."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def reverse_geocode(
        self, lat: float, lon: float
    ) -> dict[str, Any] | None:
        """Return normalised place data for (lat, lon), or None.

        Two-pass strategy:
          Pass 1 — zoom=13 (street level): precise bbox + exact settlement name.
          Pass 2 — zoom=10 (town level): only made when pass 1 returns a village
                   or hamlet.  The parent town name replaces the hamlet name while
                   the tighter zoom=13 bounding box is kept for cache accuracy.

        This means cities (Gloucester, Bristol…) make one API call; rural
        locations (Upton Scudamore → Warminster) make two.

        Returns:
            {
                "city": str,
                "boundingbox": list[str] | None,
                "osm_id": int | None,
                "place_type": str | None,
                "country": str | None,
                "country_code": str | None,
            }
        """
        result = await self._call(lat, lon, zoom=13)
        if result is None:
            return None

        if result["place_type"] in _RURAL_PLACE_TYPES:
            _LOGGER.debug(
                "(%.6f, %.6f) resolved to %s %r — trying zoom=10 for parent town",
                lat, lon, result["place_type"], result["city"],
            )
            parent = await self._call(lat, lon, zoom=10)
            if parent and parent["place_type"] not in _RURAL_PLACE_TYPES:
                _LOGGER.debug(
                    "Parent town resolved: %r → using %r as city name",
                    result["city"], parent["city"],
                )
                # Keep zoom=13 bbox (tighter / more accurate for caching)
                # but adopt the parent's human-readable name and place type.
                result["city"] = parent["city"]
                result["place_type"] = parent["place_type"]

        return result

    async def _call(
        self, lat: float, lon: float, zoom: int
    ) -> dict[str, Any] | None:
        """Make one Nominatim reverse-geocode request and return parsed result."""
        url = NOMINATIM_URL.format(lat=lat, lon=lon, zoom=zoom)
        headers = {"User-Agent": NOMINATIM_USER_AGENT}

        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=NOMINATIM_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Nominatim returned HTTP %s for (%.6f, %.6f) zoom=%d",
                        response.status, lat, lon, zoom,
                    )
                    return None
                data: dict[str, Any] = await response.json(content_type=None)

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Nominatim request timed out for (%.6f, %.6f) zoom=%d", lat, lon, zoom,
            )
            return None
        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Nominatim network error for (%.6f, %.6f) zoom=%d: %s", lat, lon, zoom, err,
            )
            return None
        except Exception:
            _LOGGER.exception(
                "Unexpected error calling Nominatim for (%.6f, %.6f) zoom=%d", lat, lon, zoom,
            )
            return None

        return self._parse_response(data, lat, lon)

    async def geocode_address(self, address: str) -> tuple[float, float] | None:
        """Forward-geocode a text address and return (lat, lon), or None.

        Used to resolve calendar event location strings like
        "NEC Birmingham, UK" to GPS coordinates.  Results are cached by the
        coordinator so this is called at most once per unique location string.
        """
        headers = {"User-Agent": NOMINATIM_USER_AGENT}
        params = {"q": address, "format": "json", "limit": "1"}

        try:
            async with self._session.get(
                NOMINATIM_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=NOMINATIM_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Nominatim search returned HTTP %s for %r",
                        response.status,
                        address,
                    )
                    return None
                results: list[dict] = await response.json(content_type=None)

        except asyncio.TimeoutError:
            _LOGGER.warning("Nominatim search timed out for %r", address)
            return None
        except aiohttp.ClientError as err:
            _LOGGER.warning("Nominatim search network error for %r: %s", address, err)
            return None
        except Exception:
            _LOGGER.exception("Unexpected error geocoding address %r", address)
            return None

        if not results:
            _LOGGER.debug("Nominatim found no results for %r", address)
            return None

        try:
            return float(results[0]["lat"]), float(results[0]["lon"])
        except (KeyError, ValueError):
            _LOGGER.warning("Unexpected Nominatim search response for %r", address)
            return None

    @staticmethod
    def _parse_response(
        data: dict[str, Any],
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any] | None:
        """Extract a normalised city result from a raw Nominatim JSON response."""
        address: dict[str, str] = data.get("address", {})

        # Log every key/value so misclassifications can be diagnosed from HA logs.
        _LOGGER.debug(
            "Nominatim address keys for (%.6f, %.6f): %s",
            lat or 0.0,
            lon or 0.0,
            {k: v for k, v in address.items()},
        )

        city_name: str | None = None
        place_type: str | None = None

        for key in _CITY_ADDRESS_KEYS:
            if key in address:
                city_name = address[key]
                place_type = key
                break

        if city_name is None:
            return None

        raw_bb = data.get("boundingbox")
        if not raw_bb or len(raw_bb) != 4:
            _LOGGER.debug("Nominatim response has missing or malformed boundingbox")
            raw_bb = None

        osm_id_raw = data.get("osm_id")
        osm_id: int | None = int(osm_id_raw) if osm_id_raw is not None else None

        return {
            "city": city_name,
            "boundingbox": raw_bb,
            "osm_id": osm_id,
            "place_type": place_type,
            "country": address.get("country"),
            "country_code": address.get("country_code", "").upper() or None,
        }
