# Immich Picture – Home Assistant Integration

A Home Assistant custom integration that turns your [Immich](https://immich.app) photo server into a rotating media slideshow, exposed as a `camera` entity. Drop it onto any dashboard with a **Picture card** or **Picture Glance card** and your photos and video previews cycle automatically.

---

## Features

- **Five media sources** – random, all recent, by album, favourites only, or metadata search
- **Filterable random & search** – POST a JSON body to the Immich API to narrow results by person, city, date range, type, and more
- **Configurable rotation** – set how often the displayed media changes (5 s – 1 h)
- **Configurable refresh** – set how often a fresh batch of assets is fetched from Immich (1 min – 24 h)
- **Resilient media cache** – every downloaded thumbnail is written to disk, and videos under 50 MiB are cached locally for playback
- **Multiple instances** – add the integration more than once to run several slideshows (e.g. one per album or one per room) simultaneously
- **Portrait photo support** – portrait images are automatically paired side-by-side to produce a landscape composite, so no photos are wasted
- **Video playback** – videos are included as JPEG previews in the camera entity and can play in a built-in, looping HTML5 player
- **Live reconfiguration** – all timing, count, and JSON filter settings are editable via the ⚙ configure button without removing and re-adding the integration

---

## Requirements

- Home Assistant 2023.x or later
- An Immich instance reachable from your HA host (local network or VPN)
- An Immich API key (generate one under **Account Settings → API Keys**)

### Immich Permissions

The following permissions are needed for all five media sources to work:

- `asset.read` - required for the random, recent, favourite and metadata search
- `album.read` - required for the album request
- `asset.view` - required to view the images in the Home Assistant entity

---

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add this repository URL and select category **Integration**
3. Search for **Immich Picture** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/immich_picture/` folder into your HA config directory under `custom_components/`
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **Immich Picture**
2. Select a saved Immich account, or enter your Immich URL (e.g. `http://192.168.1.100:2283`) and API key
3. Choose a **photo source** (see below)
4. Configure source-specific options (asset count, JSON filter, etc.)
5. Set rotation and refresh intervals
6. The integration creates a `camera` entity ready to use on any dashboard

Repeat **Add Integration** to add more sources for the same account. Choose the saved account to reuse its credentials, then configure the new source independently. Identical sources are allowed when you need separate slideshows, rotation intervals, or dashboards.

---

## Media Sources

### Random Assets

Fetches a random selection of images and video previews each refresh cycle using `POST /api/search/random`.

Supports an optional JSON filter body to narrow the random pool:

```json
{
  "personIds": ["uuid-of-person-1", "uuid-of-person-2"],
  "country": "Japan",
  "type": "VIDEO"
}
```

[API reference](https://api.immich.app/endpoints/search/searchRandom)

---

### All Assets (Recent)

Fetches the most recent assets from your library, ordered newest-first or oldest-first.

---

### Album Assets

Fetches all images and video previews from a specific album. Albums are loaded from Immich during setup so you can pick from a dropdown.

---

### Favourite Assets

Fetches only images and video previews you have marked as a favourite in Immich.

---

### Search by Metadata

Fetches images and video previews matching a JSON metadata query using `POST /api/search/metadata`. This is the most powerful source – you can filter by city, country, date range, camera make/model, whether the media is archived, a favourite, a specific person, and more.

```json
{
  "city": "Paris",
  "isFavorite": true,
  "type": "VIDEO",
  "takenAfter": "2023-01-01T00:00:00Z"
}
```

[API reference](https://api.immich.app/endpoints/search/searchAssets)

---

## Options (⚙ Configure)

After initial setup you can adjust the following via the **configure** button without restarting:

| Setting | Description | Range |
|---|---|---|
| Photo rotation interval | How often the displayed image advances | 5 s – 3600 s |
| API refresh interval | How often a fresh batch is fetched from Immich | 60 s – 86400 s |
| Number of assets | Size of the asset pool loaded per refresh | 1 – 500 |
| Filter (JSON) | JSON body for the API request *(random & search only)* | any valid JSON object |

Changes take effect immediately — the integration reloads automatically when you save.

---

## Image Cache

Every image and video thumbnail that is successfully downloaded is written to a JPEG file named for its Immich asset ID. Portrait pairs use their combined slide ID. Videos smaller than 50 MiB are also cached by Immich asset ID for local playback:

```
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.jpg
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.mp4
```

JPEG and MP4 files are retained only while their assets are in the current pool; the next Immich refresh removes stale media files and interrupted downloads. Existing numbered JPEG cache files from versions before 1.6.0 remain available during upgrade as a startup fallback, then are removed after the first successful new-format JPEG cache write.

**When Immich is available** the slot file is overwritten with the freshest image or video preview for that position. The built-in player prefers a cached MP4, falling back to Immich when a video is at least 50 MiB or has no known size.

**When Immich is unreachable** (planned downtime, network outage, server restart) the integration serves the cached file for the selected asset instead of showing a blank or incorrect image. If that asset has never been fetched before and has no cache file yet, the previously displayed image is preserved unchanged.

Deleting a configuration entry removes its entire cache directory, including JPEG previews, cached MP4 files, and unfinished downloads. You can also delete a cache directory manually; it is recreated automatically on the next successful fetch.

---

## Dashboard Usage

Add a **Picture card** or **Picture Glance card** and point it at the `camera.immich_picture_slideshow_*` entity. The image updates automatically at the configured rotation interval with no page reload required.

The entity also exposes state attributes you can use in automations or template sensors:

| Attribute | Description |
|---|---|
| `asset_id` | Immich UUID of the current photo |
| `asset_type` | Current asset type (`IMAGE` or `VIDEO`) |
| `filename` | Original file name |
| `taken_at` | Date/time the photo was taken |
| `total_assets` | Number of assets in the current pool |
| `current_index` | 1-based position in the pool |
| `endpoint` | Configured photo source identifier |
| `player_url` | Authenticated built-in media player page for this entry |

Each device also includes a **diagnostic sensor** (`sensor.immich_picture_image_cache_path`) that shows the full path to the on-disk image cache directory for that instance. This is useful for locating cached files when debugging or for manual cache management.

---

## Video Playback

The camera entity always returns a JPEG snapshot, including for VIDEO assets. To play videos, add a **Webpage** card and use the `player_url` attribute from the camera entity as its URL. The built-in player stays in sync with the slideshow: images display as snapshots, and videos play muted in a loop until the configured media rotation interval selects the next asset.

```yaml
type: iframe
url: /api/immich_picture/player/<entry-id>
aspect_ratio: "16:9"
allow: autoplay; fullscreen
disable_sandbox: true
```

Use the complete `player_url` value, including its `token` query parameter, rather than manually constructing this URL. The token grants access only to this player's current media pool, and the player proxies video requests to Immich without exposing the API key. Treat `player_url` as a secret and do not expose it through an unauthenticated reverse proxy. Videos start muted to satisfy browser autoplay policies; use the player controls to enable sound after interacting with the page.

---

## Troubleshooting

**No image shown on startup** — ensure HA can reach your Immich URL. Check **Settings → System → Logs** and filter for `immich_picture`.

**Images stop updating but the card still shows a photo** — Immich is likely unreachable; the cache is being served. The integration will resume live images automatically once Immich is back.

**"Unable to connect" during setup** – verify the URL includes the port (e.g. `http://192.168.1.100:2283`) and that the API key is correct.

**API key expired or revoked** – Home Assistant opens a reauthentication form after Immich returns `401`. Enter the replacement key once; every source using the same previous account credentials is updated and reloaded.

**JSON filter rejected** — the field must be a valid JSON *object* (curly-brace wrapper, not an array). Use a tool like [jsonlint.com](https://jsonlint.com) to validate before pasting.

---

## Changelog

### 1.6.0

- **Reliable media cache** – JPEG files now use asset IDs and atomic writes, preventing an outdated slideshow slot from being shown for another asset.
- **Lower-memory video cache** – short MP4 files are streamed to disk in chunks and cache work is cancelled safely during reload or removal.
- **Smoother player sync** – the built-in player uses the planned rotation time instead of polling Home Assistant every second.
- **Saved-account improvements** – duplicate account choices are merged, and reauthentication updates all matching sources.
- **Repository metadata** – documentation and repository links now point to this project.

### 1.5.1

- **Simplified Chinese translation** – added Chinese UI text for setup, options, errors, and the cache-path sensor.

### 1.5.0

- **Short video cache** – videos smaller than 50 MiB are cached locally for playback and removed when their source no longer includes them or its configuration entry is deleted.
- **Multiple sources per account** – new sources can reuse a saved Immich account without entering its API key again.

### 1.4.0

- **Built-in media player** – added an authenticated HTML5 player page for mixed image/video slideshows. Videos are muted and loop until the normal rotation interval advances to the next media item.

### 1.3.2

- **Video thumbnail support** – VIDEO assets are now included alongside IMAGE assets and shown as Immich-generated JPEG previews in the slideshow.

### 1.3.0

- **Portrait image support** — portrait (and square) images are no longer filtered out. Instead, they are automatically paired and composited side-by-side into a single landscape image using PIL. If there is an odd number of portrait images, the unpaired one is excluded. Landscape images continue to be served as-is.

### 1.2.1

- Initial tracked release
