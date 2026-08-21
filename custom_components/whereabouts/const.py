"""Constants for the Whereabouts integration."""

from __future__ import annotations

DOMAIN = "whereabouts"
INTEGRATION_VERSION = "1.5.1"

# Configuration keys stored in ConfigEntry.data
CONF_PERSONS = "person_entities"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PERSON_CALENDARS = "person_calendars"   # dict[person_entity_id, calendar_entity_id | None]
CONF_EVENT_RADIUS_M = "event_radius_m"
CONF_TRACK_HUBS = "track_hubs"

# Defaults
DEFAULT_SCAN_INTERVAL_MINUTES = 15
DEFAULT_SCAN_INTERVAL_SECONDS = DEFAULT_SCAN_INTERVAL_MINUTES * 60
DEFAULT_EVENT_RADIUS_M = 300
# Opt-in: hub tracking adds a dependency on a third-party API (Overpass) that
# is frequently overloaded.  Off by default so nobody inherits that silently.
DEFAULT_TRACK_HUBS = False

# Sensor state sentinels
STATE_MOVING = "moving"
STATE_UNKNOWN = "unknown"

# Event names
EVENT_CITY_ARRIVED = "whereabouts_arrived"
EVENT_CITY_DEPARTED = "whereabouts_departed"
EVENT_STARTED_MOVING = "whereabouts_started_moving"
EVENT_COUNTRY_ARRIVED = "whereabouts_country_arrived"
EVENT_COUNTRY_DEPARTED = "whereabouts_country_departed"
EVENT_CALENDAR_ARRIVED = "whereabouts_calendar_arrived"
EVENT_CALENDAR_DEPARTED = "whereabouts_calendar_departed"
EVENT_ZONE_ARRIVED = "whereabouts_zone_arrived"
EVENT_ZONE_DEPARTED = "whereabouts_zone_departed"
EVENT_HUB_ARRIVED = "whereabouts_hub_arrived"
EVENT_HUB_DEPARTED = "whereabouts_hub_departed"

# Event / attribute keys
ATTR_PERSON_ENTITY_ID = "person_entity_id"
ATTR_CITY = "city"
ATTR_PREVIOUS_CITY = "previous_city"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_PLACE_TYPE = "place_type"
ATTR_OSM_ID = "osm_id"
ATTR_COUNTRY = "country"
ATTR_COUNTRY_CODE = "country_code"
ATTR_PREVIOUS_COUNTRY = "previous_country"
ATTR_SPEED = "speed_kmh"
ATTR_SPEED_MPH = "speed_mph"
ATTR_BEARING = "bearing"
ATTR_DIRECTION = "direction"
ATTR_CALENDAR_EVENT = "calendar_event"
ATTR_ZONE = "zone"
ATTR_PREVIOUS_ZONE = "previous_zone"
ATTR_HUB = "hub"
ATTR_HUB_TYPE = "hub_type"
ATTR_HUB_CODE = "hub_code"
ATTR_PREVIOUS_HUB = "previous_hub"

# The "place" layer sits above city: one human-meaningful name resolved from
# the highest-priority source available, plus a tag saying where it came from.
# city / previous_city stay untouched by it, so the geocoded city history is
# never polluted by a night at home or a stop at an airport.
ATTR_PLACE = "place"
ATTR_PLACE_SOURCE = "place_source"

PLACE_SOURCE_CALENDAR = "calendar"
PLACE_SOURCE_ZONE = "zone"
PLACE_SOURCE_HUB = "hub"
PLACE_SOURCE_CITY = "city"
PLACE_SOURCE_MOVING = "moving"
PLACE_SOURCE_UNKNOWN = "unknown"

# HA person states that are not zone names.  Any other value *is* the
# friendly name of the zone the person is currently inside.
PERSON_STATE_HOME = "home"
PERSON_STATE_NOT_HOME = "not_home"
NON_ZONE_PERSON_STATES = frozenset(
    {PERSON_STATE_NOT_HOME, "unknown", "unavailable", "none", ""}
)

# Nominatim forward geocoding (used to resolve calendar event locations).
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim reverse-geocode URL — {zoom} is filled in by the geocoder.
# zoom=16: street level — the address hierarchy is computed for the exact
#   point, so the town/village keys are correct for where the person actually
#   is.  Lower zooms snap to the nearest place *node*, which can be km away
#   and report a neighbouring town (North Town/Aldershot instead of
#   Farnborough) with a bbox that doesn't contain the person.
# zoom=10: city/town level — used as a fallback when zoom=16 only finds a
#   village or hamlet, to resolve the parent town (e.g. Warminster).
NOMINATIM_URL = (
    "https://nominatim.openstreetmap.org/reverse"
    "?lat={lat}&lon={lon}&format=json&zoom={zoom}"
)
# Sent to both Nominatim and Overpass, as both ask for identification.
USER_AGENT = f"HomeAssistant-Whereabouts/{INTEGRATION_VERSION}"
NOMINATIM_TIMEOUT_SECONDS = 10

# Speed below which a pending city arrival is confirmed.
# Above this the person is clearly in transit even if inside the bbox.
# 10 km/h ≈ 6 mph — covers parked / walking from car, rejects all driving.
ARRIVAL_CONFIRM_SPEED_KMH = 10.0

# Minimum time a pending arrival must persist before it can be confirmed.
# Filters "arrivals" from stop-start traffic inside a town: queues and
# traffic lights clear well within this window (and driving on exits the
# bbox, discarding the pending arrival), while someone who has genuinely
# stopped is still there — and still slow — when the window elapses.
ARRIVAL_CONFIRM_DWELL_SECONDS = 300

# Maximum bounding box half-span in degrees (~11 km at UK latitudes).
# Any bbox larger than this is capped, centred on the user's GPS position,
# so the sensor can never get "stuck" inside a huge administrative area.
MAX_BBOX_DEGREES = 0.10

# Minimum bounding box half-span in degrees (~800 m at UK latitudes).
# Nominatim returns tiny bboxes for hamlets and villages; without a floor,
# indoor GPS drift (10–100 m) constantly kicks the person outside the box,
# causing rapid moving ↔ village oscillation.
MIN_BBOX_DEGREES = 0.008

# ── Transit hubs (Overpass) ───────────────────────────────────────────────
# Nominatim cannot answer "which transit hub am I in": reverse-geocoding
# inside Birmingham Airport returns the nearest road ("Elmdon Lane"), and
# layer=poi returns no result at all.  Overpass can, so hubs are fetched from
# it — but only as *enrichment*.  Every failure path falls back to the city.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Wall-clock ceiling for the HTTP request.  Overpass public instances return
# HTTP 504 "server too busy" under load often enough that this must be
# treated as an expected outcome, not an error worth retrying hard.
#
# Generous, because it costs nothing: the hub lookup runs *after* the city
# has already been published, so a slow Overpass delays only the hub name.
# A 25 s server-side budget was measured failing with "Query timed out in
# 'query' after 28 seconds" on the public instance, which is why this is
# not tighter.
OVERPASS_TIMEOUT_SECONDS = 45
# Server-side [timeout:] in the query itself — kept below the HTTP timeout.
# The bbox-scoped query completes in ~2 s when the instance is healthy, so
# this is headroom for a loaded server rather than an expected duration.
OVERPASS_QUERY_TIMEOUT_SECONDS = 30

# How far around the person to fetch hubs, once per city change.
HUB_SEARCH_RADIUS_M = 25000

# How many cities' hub lists to keep. Berlin — the densest case measured —
# is ~127 hubs after filtering, so the whole cache stays small.
HUB_CACHE_MAX_CITIES = 20

# Stations, ferry terminals and bus stations are mapped as bare OSM *nodes*
# with no extent, so containment is a radius around the point.  Airports are
# ways/relations with a real bounding box (Birmingham Airport measures
# 2.9 km x 2.3 km), which is used directly instead.
HUB_NODE_RADIUS_M = 250

# Minimum time inside a hub before arrival is announced.
#
# This — not an OSM tag — is what separates an intercity hub from a commuter
# stop.  There is no reliable "intercity" tag: ref:crs matches every UK
# commuter station (Bournville, Yardley Wood...) and uic_ref matches Berlin's
# U-Bahn.  What *is* reliable is that passing through a station on a train
# takes seconds, while actually using one as a hub takes minutes.
HUB_CONFIRM_DWELL_SECONDS = 180

# Speed above which the person is clearly still in transit through the hub
# rather than at it.  Deliberately looser than the city threshold: a train
# standing at a platform reads ~0 km/h, and a 20-minute connection is exactly
# the case worth reporting, so only genuine pass-throughs are rejected.
HUB_CONFIRM_SPEED_KMH = 30.0

# OSM station values that are local transit, never intercity.  This is the
# one clean tag distinction available, and it is what keeps the London
# Underground (station=subway) and the Overground/DLR (light_rail) out.
HUB_EXCLUDED_STATION_TYPES = ("subway", "light_rail", "tram", "monorail")

PLATFORMS = ["sensor", "device_tracker"]
