# Immich Picture – Home Assistant Integration

[简体中文](README.md) | English

Turn your [Immich](https://immich.app) library into a rotating media slideshow in Home Assistant. The integration exposes a `camera` entity for dashboard cards and includes a built-in web player for video playback.

---

## Features

- Five sources: random, all recent, album, favourites, and metadata search.
- Random and search sources accept Immich JSON filters for people, location, dates, media type, and more.
- Media rotation is configurable from 5 seconds to 1 hour; pool refresh is configurable from 1 minute to 24 hours.
- JPEG previews are cached for offline fallback; videos strictly smaller than 50 MiB can be cached as local MP4 files.
- Add independent sources for the same Immich account, including identical sources.
- Portrait and square photos are paired side-by-side. Videos use a JPEG preview in the camera and loop in the built-in web player.
- Change timing, count, and JSON filters through Configure without removing the entry.

---

## Requirements and Immich permissions

- Home Assistant 2023.x or later.
- An Immich instance reachable from the Home Assistant host.
- An Immich API key, created in **Account Settings → API Keys**.

For every source, grant `asset.read` (random, recent, favourite, and search), `album.read` (album source), and `asset.view` (viewing media).

---

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository and choose the **Integration** category.
3. Install **Immich Picture**, then restart Home Assistant.

### Manual

1. Copy `custom_components/immich_picture/` into your Home Assistant configuration directory under `custom_components/`.
2. Restart Home Assistant.

---

## Setup and multiple sources

1. Go to **Settings → Devices & Services → Add Integration** and search for **Immich Picture**.
2. Select a saved Immich account, or provide an Immich URL such as `http://192.168.1.100:2283` and its API key.
3. Choose a source, configure source-specific settings, rotation, and refresh.
4. A `camera` entity is created and ready for a dashboard.

Repeat **Add Integration** to add sources for the same account. A saved account reuses its credentials, while every source has its own entity, cache, timing, and player URL.

## Media sources

### Random media

Uses `POST /api/search/random` on every refresh. An optional JSON body narrows the pool:

```json
{
  "personIds": ["uuid-of-person-1", "uuid-of-person-2"],
  "country": "Japan",
  "type": "VIDEO"
}
```

[Immich API reference](https://api.immich.app/endpoints/search/searchRandom)

### All media (recent)

Returns library assets ordered newest-first or oldest-first.

### Album media

Returns images and video previews from a selected album. Albums are loaded from Immich during setup.

### Favourite media

Returns images and video previews marked as favourites in Immich.

### Metadata search

Uses `POST /api/search/metadata`. Filter by city, country, date, camera, archived state, favourite, person, and media type:

```json
{
  "city": "Paris",
  "isFavorite": true,
  "type": "VIDEO",
  "takenAfter": "2023-01-01T00:00:00Z"
}
```

[Immich API reference](https://api.immich.app/endpoints/search/searchAssets)

## Configurable options

| Setting | Description | Range |
|---|---|---|
| Media rotation interval | How often the current media advances | 5–3600 seconds |
| API refresh interval | How often a fresh Immich pool is fetched | 60–86400 seconds |
| Asset count | Number of assets loaded on each refresh | 1–500 |
| Filter (JSON) | API body for random and search sources | Any valid JSON object |

Saving changes reloads the integration automatically.

---

## Cache and offline fallback

Downloaded image and video previews are written as JPEG files named for their Immich asset IDs; portrait pairs use their combined slide ID. Videos strictly smaller than 50 MiB are also cached as MP4 files:

```
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.jpg
<ha-config-dir>/immich_picture/image_cache/<entry-id>/<asset-id>.mp4
```

JPEG, MP4, and interrupted-download temporary files are retained only while their assets remain in the current pool. The next refresh removes stale files. MP4 cache is limited to **500 MiB per configuration entry** and uses least-recently-used eviction, while protecting the current and in-progress downloads. Older numbered JPEG caches remain available as an upgrade fallback and are cleared after the first successful new-format write.

When Immich is reachable, previews are refreshed normally. The player prefers the local MP4; videos that are 50 MiB or larger, or whose size is unknown, are streamed through Immich. When Immich is unavailable, only the matching asset cache is used; if it has no cache, the previous image remains visible. Removing a configuration entry removes all JPEG, MP4, and temporary files for that entry. Removing a cache directory manually is also safe; it is recreated after the next successful fetch.

---

## Dashboard and entity attributes

Use `camera.immich_picture_slideshow_*` in a **Picture** or **Picture Glance** card. It advances automatically. These attributes are also available for automations and templates:

| Attribute | Description |
|---|---|
| `asset_id` | Current Immich asset UUID |
| `asset_type` | `IMAGE` or `VIDEO` |
| `filename` | Original filename |
| `taken_at` | Capture time |
| `total_assets` | Current pool size |
| `current_index` | One-based current position |
| `endpoint` | Configured source identifier |
| `player_url` | Authenticated built-in player URL |

Every device also includes `sensor.immich_picture_image_cache_path`, a diagnostic sensor showing its cache directory.

---

## Video web player

The camera always returns a JPEG snapshot, including for VIDEO assets. To play video, add a **Webpage** card and use the full `player_url` attribute from the camera. The player stays synchronized with the slideshow: images show as snapshots, and videos play muted in a loop until the configured rotation advances.

```yaml
type: iframe
url: /api/immich_picture/player/<entry-id>?token=<player-token>
aspect_ratio: "16:9"
allow: autoplay; fullscreen
disable_sandbox: true
```

Copy the complete `player_url`, including its `token`; do not construct it manually. The token is a capability URL limited to that player's current media pool. The player proxies video requests without exposing the Immich API key. Treat it as a secret and never expose it through an unauthenticated reverse proxy. Video starts muted for browser autoplay rules; users can enable sound after interacting with the controls.

If a player URL is exposed, call `immich_picture.rotate_player_token` from **Developer Tools → Actions**. Provide `entry_id` for one slideshow or omit it for all players. The old URL becomes invalid, so update the Webpage card with the regenerated `player_url` attribute.

---

## Troubleshooting

**No image at startup**: make sure Home Assistant can reach the Immich URL, then filter **Settings → System → Logs** for `immich_picture`.

**The picture remains but does not update**: Immich may be temporarily unavailable and cache fallback is active. Live updates resume automatically after reconnecting.

**Unable to connect during setup**: check that the URL includes a port, for example `http://192.168.1.100:2283`, and verify the API key.

**Expired or revoked API key**: an Immich `401` starts reauthentication. Updating the key once updates and reloads every source that used the old credentials.

**JSON filter rejected**: the field must be a JSON object, not an array. Validate it with [jsonlint.com](https://jsonlint.com) before pasting.

---

## Changelog

### 1.7.2

- Corrected `manifest.json` key ordering so Hassfest validation passes.

### 1.7.1

- **Hassfest compatibility**: declared the HTTP dependency, added the config-entry-only schema, and corrected translation structure.
- **Documentation language**: Simplified Chinese is now the default README; this complete English document remains available as [README.en.md](README.en.md).

### 1.7.0

- **Bounded video cache**: one download runs per slideshow and cached MP4 files use a 500 MiB LRU budget.
- **Safer player URLs**: token rotation, restrictive response headers, and stale-request protection in the player page.
- **More resilient API access**: request timeouts, bounded retry handling, and album asset-count enforcement.
- **Automation**: tests, CI validation, and automatic GitHub Releases for version tags.

### 1.6.0

- **Reliable media cache**: JPEG files use asset IDs and atomic writes so an outdated slideshow slot cannot appear for another asset.
- **Lower-memory video cache**: short MP4 files stream to disk and cache work is cancelled safely during reload or removal.
- **Smoother player sync**: the built-in player uses the planned rotation time instead of polling Home Assistant every second.
- **Saved-account improvements**: duplicate account choices are merged, and reauthentication updates all matching sources.

### 1.5.1

- Added Simplified Chinese UI text for setup, options, errors, and the cache-path sensor.

### 1.5.0

- Videos smaller than 50 MiB are cached locally for playback and removed when absent from the source pool or when the entry is deleted.
- New sources can reuse a saved Immich account without entering its API key again.

### 1.4.0

- Added the authenticated HTML5 player page for mixed image/video slideshows. Videos loop muted until the normal rotation advances.

### 1.3.2

- VIDEO assets are included with IMAGE assets and shown as Immich-generated JPEG previews.

### 1.3.0

- Portrait and square images are paired side-by-side; landscape images remain unchanged and an unpaired portrait is omitted.

### 1.2.1

- Initial tracked release.
