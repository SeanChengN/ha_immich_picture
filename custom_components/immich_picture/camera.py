"""Camera platform for Immich – a rotating slideshow of photos."""

from __future__ import annotations

import io
import hashlib
import hmac
import logging
import pathlib
from datetime import timedelta
from typing import Any

from aiohttp import ClientTimeout
from PIL import Image

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    API_ENDPOINTS,
    ASSET_TYPE_IMAGE,
    ASSET_TYPE_VIDEO,
    CONF_API_ENDPOINT,
    CONF_ROTATION_INTERVAL,
    DATA_CAMERAS,
    DEFAULT_ROTATION_INTERVAL,
    DOMAIN,
    MAX_VIDEO_CACHE_BYTES,
)
from .coordinator import ImmichDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Immich camera entity from a config entry."""
    coordinator: ImmichDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([ImmichCamera(coordinator, config_entry)], update_before_add=True)


class ImmichCamera(Camera):
    """A camera entity that rotates through photos returned by the Immich API.

    The entity fetches image bytes directly from Immich whenever Home Assistant
    requests a snapshot (e.g. for the dashboard picture card).  A separate
    timer advances the current photo index at the configured rotation interval
    so the displayed image changes over time without requiring a page reload.

    Each successfully downloaded thumbnail is written to a per-asset cache file
    on disk.  If Immich is unreachable, the most recent cached file for the
    requested asset is served instead, making the entity resilient to planned
    or unplanned server downtime.
    """

    _attr_has_entity_name = True
    _attr_content_type = "image/jpeg"
    # This is a read-only, non-streaming camera
    _attr_is_streaming = False
    _entity_component_unrecorded_attributes = (
        Camera._entity_component_unrecorded_attributes | frozenset({"player_url"})
    )

    def __init__(
        self,
        coordinator: ImmichDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialise the camera."""
        super().__init__()
        self._coordinator = coordinator
        self._config_entry = config_entry

        self._current_index: int = 0
        self._current_image_bytes: bytes | None = None
        self._rotation_unsubscribe = None
        self._cache_dir: pathlib.Path | None = None
        self._video_cache_in_progress: set[str] = set()

        endpoint_label = API_ENDPOINTS.get(
            config_entry.data.get(CONF_API_ENDPOINT, ""), "Immich Picture"
        )

        self._attr_name = f"Immich Picture Slideshow – {endpoint_label}"
        self._attr_unique_id = config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name="Immich Picture",
            manufacturer="Immich",
            model="Photo Server",
            configuration_url=coordinator.host,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and start the rotation timer."""
        await super().async_added_to_hass()

        # Prepare the on-disk image cache directory
        cache_dir = pathlib.Path(
            self.hass.config.path(DOMAIN, "image_cache", self._config_entry.entry_id)
        )
        await self.hass.async_add_executor_job(
            lambda: cache_dir.mkdir(parents=True, exist_ok=True)
        )
        self._cache_dir = cache_dir

        self.hass.data[DOMAIN].setdefault(DATA_CAMERAS, {})[
            self._config_entry.entry_id
        ] = self

        # Restore a displayable image immediately after startup, even if the
        # initial Immich refresh failed and no asset list is available yet.
        await self._restore_startup_image_from_cache()

        # Listen for coordinator data updates so we reset gracefully
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

        # Start the rotation timer
        rotation_interval = self._config_entry.options.get(
            CONF_ROTATION_INTERVAL,
            self._config_entry.data.get(CONF_ROTATION_INTERVAL, DEFAULT_ROTATION_INTERVAL),
        )
        self._rotation_unsubscribe = async_track_time_interval(
            self.hass,
            self._async_rotate,
            timedelta(seconds=rotation_interval),
        )

        # Load the first image
        await self._load_current_image()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the rotation timer."""
        if self._rotation_unsubscribe is not None:
            self._rotation_unsubscribe()
        cameras = self.hass.data.get(DOMAIN, {}).get(DATA_CAMERAS, {})
        if cameras.get(self._config_entry.entry_id) is self:
            cameras.pop(self._config_entry.entry_id)

    # ------------------------------------------------------------------
    # Camera interface
    # ------------------------------------------------------------------

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the bytes for the currently displayed photo."""
        return self._current_image_bytes

    # ------------------------------------------------------------------
    # State attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose metadata about the current photo."""
        asset = self.current_asset
        if asset is None:
            return {}

        assets = self._coordinator.data or []
        idx = min(self._current_index, len(assets) - 1)

        return {
            "asset_id": asset.get("id"),
            "asset_type": asset.get("type", ASSET_TYPE_IMAGE),
            "filename": asset.get("originalFileName"),
            "taken_at": asset.get("localDateTime") or asset.get("fileCreatedAt"),
            "total_assets": len(assets),
            "current_index": idx + 1,
            "endpoint": self._config_entry.data.get(CONF_API_ENDPOINT),
            "player_url": (
                f"/api/{DOMAIN}/player/{self._config_entry.entry_id}"
                f"?token={self.player_token}"
            ),
        }

    @property
    def current_asset(self) -> dict[str, Any] | None:
        """Return the media asset currently selected for the slideshow."""
        assets = self._coordinator.data or []
        if not assets:
            return None
        return assets[min(self._current_index, len(assets) - 1)]

    def has_video_asset(self, asset_id: str) -> bool:
        """Return whether an asset is a video in the current media pool."""
        return any(
            asset.get("id") == asset_id and asset.get("type") == ASSET_TYPE_VIDEO
            for asset in self._coordinator.data or []
        )

    @property
    def player_token(self) -> str:
        """Return a stable opaque token for the embedded player URL."""
        return hmac.new(
            self._coordinator.api_key.encode(),
            self._config_entry.entry_id.encode(),
            hashlib.sha256,
        ).hexdigest()

    def is_valid_player_token(self, token: str | None) -> bool:
        """Validate an embedded player token without exposing the API key."""
        return bool(token) and hmac.compare_digest(token, self.player_token)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a fresh batch of assets from the coordinator."""
        assets = self._coordinator.data or []
        if assets and self._current_index >= len(assets):
            self._current_index = 0

        # Schedule fetching the new current image without blocking the callback
        self.hass.async_create_task(self._load_current_image())

        # Prune any slot files beyond the new asset count
        if self._cache_dir is not None:
            self.hass.async_create_task(self._prune_cache(len(assets), assets))

        self.async_write_ha_state()

    async def _async_rotate(self, _now=None) -> None:
        """Advance to the next photo in the list."""
        assets = self._coordinator.data
        if not assets:
            return

        self._current_index = (self._current_index + 1) % len(assets)
        await self._load_current_image()
        self.async_write_ha_state()

    async def _prune_cache(self, keep: int, assets: list[dict[str, Any]]) -> None:
        """Prune obsolete image slots and video files outside the asset pool."""
        video_ids = {
            str(asset["id"])
            for asset in assets
            if asset.get("type") == ASSET_TYPE_VIDEO and asset.get("id")
        }

        def _delete_excess() -> None:
            for f in self._cache_dir.glob("*.jpg"):
                try:
                    if int(f.stem) >= keep:
                        f.unlink()
                except (ValueError, OSError):
                    pass

            for f in self._cache_dir.glob("*.mp4"):
                try:
                    if f.stem not in video_ids:
                        f.unlink()
                except OSError:
                    pass

            for f in self._cache_dir.glob("*.mp4.part"):
                try:
                    asset_id = f.name.removesuffix(".mp4.part")
                    if asset_id not in video_ids:
                        f.unlink()
                except OSError:
                    pass

        await self.hass.async_add_executor_job(_delete_excess)

    async def _fetch_single_thumbnail(self, asset_id: str) -> bytes | None:
        """Fetch a single thumbnail from Immich, returning raw bytes or None."""
        url = (
            f"{self._coordinator.host}/api/assets/{asset_id}/thumbnail"
            "?size=preview&edited=true"
        )
        session = async_get_clientsession(self.hass)
        async with session.get(
            url,
            headers={"x-api-key": self._coordinator.api_key},
            timeout=15,
        ) as resp:
            if resp.status == 200:
                return await resp.read()
            _LOGGER.warning(
                "Immich returned HTTP %s for asset thumbnail %s",
                resp.status,
                asset_id,
            )
            return None

    def cached_video_path(self, asset_id: str) -> pathlib.Path | None:
        """Return a completed local video cache file, if one exists."""
        if self._cache_dir is None:
            return None
        cache_file = self._cache_dir / f"{asset_id}.mp4"
        return cache_file if cache_file.is_file() else None

    def _schedule_video_cache(self, asset_id: str) -> None:
        """Start caching the current short video without blocking rotation."""
        if asset_id in self._video_cache_in_progress:
            return
        if self.cached_video_path(asset_id) is not None:
            return
        self._video_cache_in_progress.add(asset_id)
        self.hass.async_create_task(self._async_cache_video(asset_id))

    async def _async_cache_video(self, asset_id: str) -> None:
        """Cache a video only when Immich reports it is below the size limit."""
        if self._cache_dir is None:
            self._video_cache_in_progress.discard(asset_id)
            return

        cache_file = self._cache_dir / f"{asset_id}.mp4"
        temp_file = self._cache_dir / f"{asset_id}.mp4.part"
        session = async_get_clientsession(self.hass)
        url = f"{self._coordinator.host}/api/assets/{asset_id}/video/playback"
        headers = {"x-api-key": self._coordinator.api_key, "Range": "bytes=0-0"}

        try:
            async with session.get(url, headers=headers, timeout=15) as resp:
                content_range = resp.headers.get("Content-Range", "")
                total_text = content_range.rsplit("/", 1)[-1]
                if resp.status != 206 or not total_text.isdecimal():
                    return
                if int(total_text) >= MAX_VIDEO_CACHE_BYTES:
                    return

            async with session.get(
                url,
                headers={"x-api-key": self._coordinator.api_key},
                timeout=ClientTimeout(total=None, sock_connect=15),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.read()

            if len(data) >= MAX_VIDEO_CACHE_BYTES:
                return

            def _write_cache() -> None:
                temp_file.write_bytes(data)
                temp_file.replace(cache_file)

            await self.hass.async_add_executor_job(_write_cache)
            _LOGGER.debug("Cached Immich video %s", asset_id)
        except Exception as err:
            _LOGGER.debug("Could not cache Immich video %s: %s", asset_id, err)
            try:
                await self.hass.async_add_executor_job(temp_file.unlink, True)
            except FileNotFoundError:
                pass
        finally:
            self._video_cache_in_progress.discard(asset_id)

    @staticmethod
    def _compose_side_by_side(left_bytes: bytes, right_bytes: bytes) -> bytes:
        """Stitch two portrait images side-by-side into a single landscape image."""
        left_img = Image.open(io.BytesIO(left_bytes))
        right_img = Image.open(io.BytesIO(right_bytes))

        # Scale both images to the same height (use the smaller height)
        target_h = min(left_img.height, right_img.height)
        if left_img.height != target_h:
            scale = target_h / left_img.height
            left_img = left_img.resize(
                (int(left_img.width * scale), target_h), Image.LANCZOS
            )
        if right_img.height != target_h:
            scale = target_h / right_img.height
            right_img = right_img.resize(
                (int(right_img.width * scale), target_h), Image.LANCZOS
            )

        combined = Image.new("RGB", (left_img.width + right_img.width, target_h))
        combined.paste(left_img, (0, 0))
        combined.paste(right_img, (left_img.width, 0))

        buf = io.BytesIO()
        combined.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    async def _load_current_image(self) -> None:
        """Fetch image bytes for the current asset from Immich.

        On success the bytes are written to disk so they can be served if
        Immich is unavailable during a later rotation cycle.  On any failure
        the cached file for this asset is used instead; if no cache exists the
        last in-memory image is preserved unchanged.
        """
        assets = self._coordinator.data
        if not assets:
            return

        idx = min(self._current_index, len(assets) - 1)
        asset = assets[idx]
        asset_id = asset.get("id")
        if not asset_id:
            return

        cache_file = (
            self._cache_dir / f"{idx}.jpg" if self._cache_dir is not None else None
        )

        try:
            if asset.get("is_portrait_pair"):
                # Fetch both portrait images and composite them
                left_id = asset["left"]["id"]
                right_id = asset["right"]["id"]
                left_bytes = await self._fetch_single_thumbnail(left_id)
                right_bytes = await self._fetch_single_thumbnail(right_id)

                if left_bytes and right_bytes:
                    data = await self.hass.async_add_executor_job(
                        self._compose_side_by_side, left_bytes, right_bytes
                    )
                    self._current_image_bytes = data
                    if cache_file is not None:
                        await self.hass.async_add_executor_job(
                            cache_file.write_bytes, data
                        )
                else:
                    _LOGGER.warning(
                        "Could not fetch both portrait thumbnails for pair %s",
                        asset_id,
                    )
                    await self._serve_from_cache(cache_file)
            else:
                # Single landscape image
                data = await self._fetch_single_thumbnail(asset_id)
                if data:
                    self._current_image_bytes = data
                    if cache_file is not None:
                        await self.hass.async_add_executor_job(
                            cache_file.write_bytes, data
                        )
                    if asset.get("type") == ASSET_TYPE_VIDEO:
                        self._schedule_video_cache(asset_id)
                else:
                    await self._serve_from_cache(cache_file)
        except Exception as err:
            _LOGGER.debug("Error fetching Immich thumbnail for %s: %s", asset_id, err)
            await self._serve_from_cache(cache_file)

    async def _restore_startup_image_from_cache(self) -> None:
        """Restore the first available cached image during Home Assistant startup."""
        if self._cache_dir is None or self._current_image_bytes is not None:
            return

        cache_file = await self.hass.async_add_executor_job(
            self._find_first_cached_image
        )
        if cache_file is not None:
            await self._serve_from_cache(cache_file)

    def _find_first_cached_image(self) -> pathlib.Path | None:
        """Return the lowest-numbered cached slot file, if any exists."""
        if self._cache_dir is None:
            return None

        candidates: list[tuple[int, pathlib.Path]] = []
        for cache_file in self._cache_dir.glob("*.jpg"):
            try:
                candidates.append((int(cache_file.stem), cache_file))
            except ValueError:
                continue

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    async def _serve_from_cache(self, cache_file: pathlib.Path | None) -> None:
        """Load image bytes from the on-disk cache for this asset, if available."""
        if cache_file is None:
            return
        try:
            data = await self.hass.async_add_executor_job(cache_file.read_bytes)
            self._current_image_bytes = data
            _LOGGER.debug("Serving cached image: %s", cache_file.name)
        except FileNotFoundError:
            pass  # No cache yet for this asset; keep the last in-memory image
        except OSError as err:
            _LOGGER.debug("Could not read cache file %s: %s", cache_file, err)
