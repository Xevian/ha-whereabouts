"""Place-name selection from Nominatim address payloads.

    python tests/test_geocoder.py

Nominatim's `address` block is a hierarchy, not a label, and in England the
same key means two different things depending on where you stand.  In a
two-tier council area "city" holds the *district*: a point on the A40 outside
Gloucester reported as "Tewkesbury", a borough whose town centre is 15 km
north, while the settlement the point sits in is under "town", "suburb",
"village" or "hamlet".  In a real city "city" holds the city.

The address blocks below are recorded verbatim from the live API so the
selection rules can be exercised without a network round trip.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_hass import load  # noqa: E402

geocoder = load("geocoder")
Geocoder = geocoder.NominatimGeocoder


def _response(address: dict, osm_id: int = 1) -> dict:
    """Wrap an address block in the rest of a Nominatim reverse response."""
    return {
        "osm_id": osm_id,
        "boundingbox": ["51.87", "51.88", "-2.29", "-2.27"],
        "address": address,
    }


# ── recorded address blocks (zoom=16) ─────────────────────────────────────
# Rural: "city" is the surrounding borough, not where the person is.
A40_NEAR_HIGHNAM = {
    "road": "A40", "hamlet": "Linton", "suburb": "Highnam",
    "village": "Oakridge", "city": "Tewkesbury", "county": "Gloucestershire",
    "state": "England", "postcode": "GL2 8DF",
    "country": "United Kingdom", "country_code": "gb",
}
OVER = {
    "road": "A40", "suburb": "Over", "village": "Oakridge",
    "city": "Tewkesbury", "county": "Gloucestershire",
    "country": "United Kingdom", "country_code": "gb",
}
MAISEMORE = {
    "road": "A417", "hamlet": "Maisemore Ham", "suburb": "Maisemore",
    "village": "Maisemore", "city": "Tewkesbury",
    "country": "United Kingdom", "country_code": "gb",
}
# Rural with no suburb — the village itself is the answer.
VILLAGE_ONLY = {
    "road": "Church Lane", "village": "Ashleworth", "city": "Tewkesbury",
    "country": "United Kingdom", "country_code": "gb",
}

# Urban: "city" is the city.  No village/hamlet key anywhere.
GLOUCESTER = {
    "road": "Metz Way", "neighbourhood": "Kings Barton", "suburb": "Barton",
    "city": "Gloucester", "county": "Gloucestershire",
    "country": "United Kingdom", "country_code": "gb",
}
SOHO = {
    "road": "Poland Street", "suburb": "Soho", "city": "City of Westminster",
    "country": "United Kingdom", "country_code": "gb",
}

# Unitary authority (Wiltshire): no district ever reaches the "city" key, so
# the address is just a town.  Nothing about the district rule should touch it.
WARMINSTER = {
    "road": "Roman Way", "town": "Warminster", "county": "Wiltshire",
    "state": "England", "postcode": "BA12 8PE",
    "country": "United Kingdom", "country_code": "gb",
}
# Open country in the same unitary authority — a village with no "city" key at
# all.  The demotion must stay keyed on "city" being present, not on the mere
# presence of a village, or this starts behaving differently for no reason.
UPTON_SCUDAMORE = {
    "road": "Warminster Road", "suburb": "Upton Scudamore",
    "village": "Upton Scudamore", "county": "Wiltshire",
    "country": "United Kingdom", "country_code": "gb",
}

# Two-tier with a "town" key — the case the original fix covered.
FARNBOROUGH = {
    "road": "Oak Road", "residential": "Farnborough Park",
    "town": "Farnborough", "city": "Rushmoor", "county": "Hampshire",
    "country": "United Kingdom", "country_code": "gb",
}
# A town that also carries a village key: "town" must still win, or Farnham
# town centre reports as the neighbouring village of Hale.
FARNHAM = {
    "road": "Borelli Yard", "neighbourhood": "Fairfield", "town": "Farnham",
    "village": "Hale", "city": "Waverley", "county": "Surrey",
    "country": "United Kingdom", "country_code": "gb",
}


def test_borough_in_city_key_loses_to_the_local_settlement():
    result = Geocoder._parse_response(_response(A40_NEAR_HIGHNAM))
    assert result["city"] == "Highnam", result["city"]
    assert result["place_type"] == "suburb", result["place_type"]


def test_borough_is_flagged_so_the_parent_pass_can_be_skipped():
    assert Geocoder._parse_response(_response(A40_NEAR_HIGHNAM))["city_is_district"]
    assert not Geocoder._parse_response(_response(GLOUCESTER))["city_is_district"]


def test_neighbouring_rural_points_keep_their_own_names():
    for address, expected in ((OVER, "Over"), (MAISEMORE, "Maisemore")):
        result = Geocoder._parse_response(_response(address))
        assert result["city"] == expected, f"{expected}: {result['city']}"


def test_village_wins_when_there_is_no_suburb():
    result = Geocoder._parse_response(_response(VILLAGE_ONLY))
    assert result["city"] == "Ashleworth", result["city"]
    assert result["place_type"] == "village", result["place_type"]


def test_real_cities_are_not_demoted_to_their_suburbs():
    for address, expected in ((GLOUCESTER, "Gloucester"), (SOHO, "City of Westminster")):
        result = Geocoder._parse_response(_response(address))
        assert result["city"] == expected, f"{expected}: {result['city']}"
        assert result["place_type"] == "city", result["place_type"]


def test_town_still_outranks_the_district():
    for address, expected in ((FARNBOROUGH, "Farnborough"), (FARNHAM, "Farnham")):
        result = Geocoder._parse_response(_response(address))
        assert result["city"] == expected, f"{expected}: {result['city']}"
        assert result["place_type"] == "town", result["place_type"]


def test_unitary_authority_points_are_untouched():
    """Wiltshire has no second tier, so these never saw the bug — keep it so."""
    for address, expected, kind in (
        (WARMINSTER, "Warminster", "town"),
        (UPTON_SCUDAMORE, "Upton Scudamore", "suburb"),
    ):
        result = Geocoder._parse_response(_response(address))
        assert result["city"] == expected, f"{expected}: {result['city']}"
        assert result["place_type"] == kind, result["place_type"]
        assert not result["city_is_district"]


def test_country_is_carried_through():
    result = Geocoder._parse_response(_response(A40_NEAR_HIGHNAM))
    assert result["country"] == "United Kingdom"
    assert result["country_code"] == "GB"


def test_address_with_no_place_key_returns_none():
    assert Geocoder._parse_response(_response({"road": "A40"})) is None


# ── two-pass behaviour ────────────────────────────────────────────────────
def _geocoder_returning(by_zoom: dict[int, dict | None]):
    """A geocoder whose _call serves canned results and records the zooms."""
    instance = Geocoder(session=None)
    calls: list[int] = []

    async def _call(lat, lon, zoom):
        calls.append(zoom)
        return by_zoom.get(zoom)

    instance._call = _call
    return instance, calls


def test_parent_pass_is_skipped_when_the_city_is_a_district():
    """zoom=10 only ever snaps back to the borough, so don't spend the call."""
    village = Geocoder._parse_response(_response(VILLAGE_ONLY))
    instance, calls = _geocoder_returning({10: None, 16: village})

    result = asyncio.run(instance.reverse_geocode(51.87, -2.28))

    assert calls == [16], calls
    assert result["city"] == "Ashleworth", result["city"]


def test_parent_pass_still_runs_for_a_village_with_a_real_parent_town():
    hamlet = Geocoder._parse_response(
        _response({"hamlet": "Upton Scudamore", "country_code": "gb"})
    )
    town = Geocoder._parse_response(
        _response({"town": "Warminster", "country_code": "gb"})
    )
    instance, calls = _geocoder_returning({10: town, 16: hamlet})

    result = asyncio.run(instance.reverse_geocode(51.23, -2.19))

    assert calls == [16, 10], calls
    assert result["city"] == "Warminster", result["city"]
    assert result["place_type"] == "town", result["place_type"]


def test_parent_pass_result_is_rejected_when_it_is_a_district():
    hamlet = Geocoder._parse_response(
        _response({"hamlet": "Linton", "country_code": "gb"})
    )
    district = Geocoder._parse_response(_response(A40_NEAR_HIGHNAM))
    district["city"] = "Tewkesbury"
    district["place_type"] = "city"
    instance, _calls = _geocoder_returning({10: district, 16: hamlet})

    result = asyncio.run(instance.reverse_geocode(51.87, -2.28))

    assert result["city"] == "Linton", result["city"]


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = []
    for name, test in tests:
        try:
            test()
        except AssertionError as err:
            failures.append(name)
            print(f"FAIL  {name}\n      {err}")
        except Exception as err:  # noqa: BLE001 - a crash is a failure too
            failures.append(name)
            print("ERROR " + name)
            print("      " + type(err).__name__ + ": " + str(err))
        else:
            print(f"pass  {name}")

    print()
    if failures:
        print(f"{len(failures)} of {len(tests)} failed")
        return 1
    print(f"all {len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
