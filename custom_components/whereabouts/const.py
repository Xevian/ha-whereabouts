"""Constants for the Whereabouts integration."""

from __future__ import annotations

DOMAIN = "whereabouts"
INTEGRATION_VERSION = "1.0.0"

# Configuration keys stored in ConfigEntry.data
CONF_PERSONS = "person_entities"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_PERSON_CALENDARS = "person_calendars"   # dict[person_entity_id, calendar_entity_id | None]
CONF_EVENT_RADIUS_M = "event_radius_m"

# Defaults
DEFAULT_SCAN_INTERVAL_MINUTES = 15
DEFAULT_SCAN_INTERVAL_SECONDS = DEFAULT_SCAN_INTERVAL_MINUTES * 60
DEFAULT_EVENT_RADIUS_M = 300

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

# Nominatim forward geocoding (used to resolve calendar event locations).
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim — zoom=13 gives street/suburb level results.
# zoom=10 returns admin districts like "Greater London" or "Cotswold District";
# zoom=12 can still return borough names (e.g. "Tewkesbury" instead of "Gloucester");
# zoom=13 is more specific and returns the actual settlement name reliably.
NOMINATIM_URL = (
    "https://nominatim.openstreetmap.org/reverse"
    "?lat={lat}&lon={lon}&format=json&zoom=13"
)
NOMINATIM_USER_AGENT = f"HomeAssistant-Whereabouts/{INTEGRATION_VERSION}"
NOMINATIM_TIMEOUT_SECONDS = 10

# Speed below which a pending city arrival is confirmed.
# Above this the person is clearly in transit even if inside the bbox.
# 25 km/h ≈ 15 mph — covers slow traffic / parking, rejects motorway driving.
ARRIVAL_CONFIRM_SPEED_KMH = 25.0

# Maximum bounding box half-span in degrees (~11 km at UK latitudes).
# Any bbox larger than this is capped, centred on the user's GPS position,
# so the sensor can never get "stuck" inside a huge administrative area.
MAX_BBOX_DEGREES = 0.10

PLATFORMS = ["sensor", "device_tracker"]
