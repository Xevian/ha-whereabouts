# Whereabouts

[![HACS Custom][hacs-badge]][hacs-url]
[![GitHub Release][release-badge]][release-url]
[![Validate][validate-badge]][validate-url]
[![License: MIT][license-badge]][license-url]

A [HACS](https://hacs.xyz)-compatible Home Assistant custom integration that gives you rich location awareness for any `person` entity — **which zone, which city, which country, how fast they're moving, which direction, and whether they're near a calendar event venue**.

---

## Features

| | |
|---|---|
| 🏙️ **City / town tracking** | Reverse-geocodes GPS coordinates via Nominatim and reports the nearest town or city |
| 🌍 **Country tracking** | Detects country arrivals and departures as a separate attribute |
| 🗺️ **Bounding-box cache** | Uses Nominatim's own bounding box — no fixed radius, no constant API calls |
| 🚗 **Speed & bearing** | Calculates speed (km/h + mph), compass bearing, and 8-point direction between GPS updates |
| 🚉 **Transit hubs** | Optional: names the airport, mainline station, ferry port or bus station you're at, via Overpass — pure enrichment, falls back to the city if unavailable |
| 🏠 **Zone awareness** | Reports the name of the HA zone you're in — "Home" and "Work" beat a geocoded city name for the same spot |
| 📅 **Calendar event proximity** | Links a Google (or any HA) calendar per person; sensor state switches to the event title when within the arrival radius |
| 📍 **Next event map pin** | A `device_tracker` entity shows the next calendar event *with a location* as a map pin with an arrival-radius circle |
| 🔔 **HA bus events** | Fires `whereabouts_arrived`, `whereabouts_departed`, `whereabouts_country_arrived`, `whereabouts_country_departed`, `whereabouts_calendar_arrived`, `whereabouts_calendar_departed`, `whereabouts_zone_arrived`, `whereabouts_zone_departed`, `whereabouts_hub_arrived`, `whereabouts_hub_departed` for automations |
| ⚙️ **Options flow** | Change persons, scan interval, arrival radius, or calendar assignments at any time without restarting HA |

---

## Requirements

- Home Assistant **2024.7.0** or later (required by `async_register_static_paths`)
- [HACS](https://hacs.xyz) (recommended install method)
- At least one `person` entity with a GPS-capable device tracker

---

## Installation

### Via HACS (recommended)

1. In HA, go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/Xevian/ha-whereabouts` with category **Integration**
3. Search for **Whereabouts** and install
4. Restart Home Assistant

### Manual

1. Copy `custom_components/whereabouts/` into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **Whereabouts**
2. Select the person entities to track and set the scan interval (default 15 min)
3. Set the calendar event arrival radius in metres (default 300 m)
4. Optionally link a calendar entity to each person (for event proximity tracking)

---

## Entities

For each tracked person (e.g. `person.john`) the integration creates:

| Entity | Type | State |
|---|---|---|
| `sensor.whereabouts_john` | Sensor | Zone · event title · transit hub · city name · `moving` · `unknown` |
| `device_tracker.whereabouts_event_john` | Device Tracker | Next calendar event title (map pin) |

### Sensor attributes

| Attribute | Description |
|---|---|
| `place_source` | Which layer produced the state: `zone`, `calendar`, `hub`, `city`, `moving`, `unknown` |
| `zone` | Current HA zone name, if inside one |
| `previous_zone` | Previous HA zone |
| `hub` | Current transit hub name, if at one |
| `hub_type` | `airport`, `station`, `ferry` or `bus` |
| `hub_code` | IATA / CRS / UIC reference where tagged (e.g. `BHX`, `BHI`) |
| `previous_hub` | Previous transit hub |
| `city` | Current city / town — always the geocoded city, never overwritten by a zone |
| `previous_city` | Previous city |
| `country` | Country name |
| `country_code` | ISO 3166-1 alpha-2 code |
| `place_type` | Nominatim place type (city, town, village…) |
| `latitude` / `longitude` | Last known GPS coordinates |
| `speed_kmh` / `speed_mph` | Speed between last two GPS updates |
| `bearing` | Compass bearing in degrees |
| `direction` | 8-point compass direction (N, NE, E…) |
| `calendar_event` | Active event title when within arrival radius |
| `osm_id` | OpenStreetMap relation ID for the current place |

### Device tracker (map pin)

The `device_tracker` entity shows the **next upcoming calendar event that has a location** as a map pin. The accuracy circle on the map represents the configured arrival radius. It scans up to 90 days ahead, skipping events without a location, so the pin always points at something meaningful.

---

## Events

All events are fired on the HA event bus and can be used as automation triggers.

### `whereabouts_arrived`
```yaml
person_entity_id: person.john
city: Paris
previous_city: Lyon       # null on first detection after HA restart
country: France
country_code: FR
latitude: 48.8566
longitude: 2.3522
```

### `whereabouts_departed`
```yaml
person_entity_id: person.john
city: Paris
country: France
latitude: 48.8566
longitude: 2.3522
```

### `whereabouts_country_arrived` / `whereabouts_country_departed`
```yaml
person_entity_id: person.john
country: France
country_code: FR
previous_country: Spain   # null on first detection (country_arrived only)
latitude: 48.8566
longitude: 2.3522
```

### `whereabouts_zone_arrived` / `whereabouts_zone_departed`
```yaml
person_entity_id: person.john
zone: Home
previous_zone: Work       # zone_arrived only; null on first detection
latitude: 51.8642
longitude: -2.2380
```

Zone events fire immediately on the transition. Unlike city arrivals they need
no dwell or speed confirmation — HA has already decided the person is inside
the zone, and its own zone logic accounts for GPS accuracy.

### `whereabouts_hub_arrived` / `whereabouts_hub_departed`
```yaml
person_entity_id: person.john
hub: Birmingham Airport
hub_type: airport        # airport | station | ferry | bus
hub_code: BHX            # IATA / CRS / UIC, null when untagged
previous_hub: Gloucester Station   # hub_arrived only
latitude: 52.4536
longitude: -1.7433
```

Only fired when hub tracking is enabled.

### `whereabouts_calendar_arrived` / `whereabouts_calendar_departed`
```yaml
person_entity_id: person.john
calendar_event: "MCM Comic Con Birmingham"
latitude: 52.4500
longitude: -1.7240
```

---

## Automation examples

See [`automations_example.yaml`](automations_example.yaml) for 15 ready-to-use automations covering:

- Arrival / departure notifications for any person or city
- Zone arrival notifications, and city alerts that ignore zone-sourced states
- Transit hub arrivals, with an airport-only variant for travel automations
- Country landing alerts
- Calendar event arrival / departure notifications
- Sensor state conditions (e.g. turn on lights when in a specific city)

---

## Options

After setup, go to **Settings → Devices & Services → Whereabouts → Configure** to:

- **Change settings** — update tracked persons, scan interval, arrival radius, or transit hub tracking
- **Change calendars** — reassign or remove the calendar linked to each person

---

## How it works

### The place layer

The sensor state is one *place* name, resolved from the highest-priority
source that has an answer:

| Priority | Source | `place_source` | Example |
|---|---|---|---|
| 1 | Active calendar event within the arrival radius | `calendar` | `MCM Comic Con Birmingham` |
| 2 | HA zone the person is inside | `zone` | `Home` |
| 3 | Transit hub the person is at | `hub` | `Birmingham Airport` |
| 4 | Reverse-geocoded city | `city` | `Gloucester` |
| 5 | Outside the cached bbox | `moving` | `moving` |

A zone outranks a hub deliberately: if you have drawn a zone around your local
station, you named it, and that name wins.

The `city` attribute is never overwritten by the layers above it, so
`previous_city` and the `whereabouts_arrived` / `whereabouts_departed` chain
stay a record of real geocoded cities — a week at home doesn't push `Home`
into your city history.

Zones cost nothing to resolve: HA already writes the zone's friendly name into
the person entity's state, so no geometry runs and no API is called. The one
exception is **passive zones**, which HA deliberately excludes from person
state; those are matched by distance, and only when the person is otherwise
`not_home`.

### Transit hubs

Off by default — enable **Track transit hubs** in the integration options.

Reverse geocoding cannot answer "which hub am I in". Standing in the Birmingham
Airport terminal, Nominatim returns `Elmdon Lane` at `zoom=16`, a taxiway
holding-position marker at `zoom=18`, and nothing at all for `layer=poi`. So
hubs come from [Overpass](https://overpass-api.de) instead, which queries OSM
by tag.

**One Overpass call per city change**, piggybacking a Nominatim call already
being made, cached against the city's `osm_id`. Never once per GPS update — the
proximity test runs against the cached list in memory.

**The lookup runs after the city is published.** The sensor shows the city
first; the hub name lands a moment later if it lands at all. A slow Overpass
can never delay the city.

The query is scoped by a global `[bbox:]` rather than per-clause `around:`, and
uses `node` for everything except aerodromes. Both matter: the `around` form
reliably exceeded a 50-second server budget, while the bbox form returns in
about two seconds.

**Failure is expected and silent.** Public Overpass instances return HTTP 504
under load routinely. When a lookup fails the previous hub list is kept, the
sensor reports the city, and nothing else changes. Hub data is enrichment; it
is never allowed to degrade city tracking.

**Geometry differs by hub type.** Airports are OSM relations with a real
bounding box — Birmingham Airport measures 2.90 km × 2.29 km — and are tested
against it. Stations, ferry terminals and bus stations are mapped as bare
nodes, so those use a 250 m radius. Where hubs overlap, the smaller one wins.

**What counts as a hub.** Airports must carry an IATA code, which excludes
gliding clubs and airstrips. Stations exclude `subway`, `light_rail`, `tram`
and `monorail`, which is what keeps the London Underground, the Overground and
the DLR out.

There is deliberately no attempt to select "intercity" stations by tag, because
no such tag works: `ref:crs` matches every UK commuter halt (Bournville, Small
Heath, Yardley Wood…) and `uic_ref` matches the Berlin U-Bahn. **Dwell time does
that filtering instead** — arrival is only announced once you have been inside
the hub for 3 minutes and are below 30 km/h. Passing through a station on a
train takes seconds; using one as a hub takes minutes. The trade-off is that a
long stop near a small commuter station will name it, which `place_source: hub`
lets automations filter on.

### Update flow

1. **Real-time updates** — `async_track_state_change_event` fires on every GPS position change of a tracked person
2. **Bounding-box cache** — if the new position falls inside the cached bbox for the current city, no API call is made
3. **Hub lookup** — on a city change only, and only when hub tracking is on
4. **Nominatim reverse geocode** — only called when the person leaves the cached bbox (zoom=16 street level, so the town is resolved for the exact point; zoom=10 fallback resolves the parent town of villages/hamlets)
4. **MAX_BBOX cap** — bboxes larger than ~11 km are capped to prevent getting "stuck" in large administrative areas
5. **Calendar proximity** — haversine distance to the next geocoded event location; switches sensor state and fires event when within radius
6. **Speed / bearing** — calculated from successive GPS coordinates and elapsed time using the haversine formula

---

## Privacy

Whereabouts sends reverse-geocode requests to [Nominatim (OpenStreetMap)](https://nominatim.openstreetmap.org/). With hub tracking enabled it also sends coordinates to [Overpass](https://overpass-api.de/), at most once per city change. Requests include only latitude and longitude — no personal identifiers. Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/) applies (max 1 req/second, which this integration respects by design).

---

## License

[MIT](LICENSE)

---

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-orange.svg
[hacs-url]: https://hacs.xyz
[release-badge]: https://img.shields.io/github/v/release/Xevian/ha-whereabouts
[release-url]: https://github.com/Xevian/ha-whereabouts/releases
[validate-badge]: https://github.com/Xevian/ha-whereabouts/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/Xevian/ha-whereabouts/actions/workflows/validate.yml
[license-badge]: https://img.shields.io/badge/License-MIT-blue.svg
[license-url]: https://github.com/Xevian/ha-whereabouts/blob/master/LICENSE
