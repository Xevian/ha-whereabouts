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
# "suburb" sits between city and town: at zoom=12 it catches London
# neighbourhoods (Westminster, Hackney…) that would otherwise return the
# large "Greater London" city entry.
_CITY_ADDRESS_KEYS = ("city", "suburb", "town", "village", "hamlet")


class NominatimGeocoder:
    """Async reverse geocoder backed by Nominatim (zoom=10, city/town level)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def reverse_geocode(
        self, lat: float, lon: float
    ) -> dict[str, Any] | None:
        """Return normalised place data for (lat, lon), or None.

        Returns:
            {
                "city": str,
                "boundingbox": list[str] | None,  # [min_lat, max_lat, min_lon, max_lon]
                "osm_id": int | None,
                "place_type": str | None,          # "city", "town", "village", "hamlet"
                "country": str | None,             # e.g. "France"
                "country_code": str | None,        # ISO 3166-1 alpha-2, e.g. "fr"
            }
        """
        url = NOMINATIM_URL.format(lat=lat, lon=lon)
        headers = {"User-Agent": NOMINATIM_USER_AGENT}

        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=NOMINATIM_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    _LOGGER.warning(
                        "Nominatim returned HTTP %s for (%.6f, %.6f)",
                        response.status,
                        lat,
                        lon,
                    )
                    return None
                # content_type=None bypasses the MIME check — Nominatim
                # occasionally returns text/html even for valid JSON payloads.
                data: dict[str, Any] = await response.json(content_type=None)

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Nominatim request timed out for (%.6f, %.6f)", lat, lon
            )
            return None
        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Nominatim network error for (%.6f, %.6f): %s", lat, lon, err
            )
            return None
        except Exception:
            _LOGGER.exception(
                "Unexpected error calling Nominatim for (%.6f, %.6f)", lat, lon
            )
            return None

        return self._parse_response(data)

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
    def _parse_response(data: dict[str, Any]) -> dict[str, Any] | None:
        """Extract a normalised city result from a raw Nominatim JSON response."""
        address: dict[str, str] = data.get("address", {})

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
