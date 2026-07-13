"""Camera platform for Immich – a rotating slideshow of photos."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import io
import logging
import pathlib
import time
import uuid
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
    CONF_PLAYER_TOKEN_SALT,
    CONF_ROTATION_INTERVAL,
    DATA_CAMERAS,
    DEFAULT_ROTATION_INTERVAL,
    DOMAIN,
    MAX_VIDEO_CACHE_BYTES,
    MAX_VIDEO_CACHE_TOTAL_BYTES,
)
from .cache import video_files_to_evict
from .coordinator import ImmichDataUpdateCoordinator
from .tokens import create_player_token

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
        self._rotation_interval = DEFAULT_ROTATION_INTERVAL
        self._next_rotation_at: float | None = None
        self._cache_dir: pathlib.Path | None = None
        self._media_revision = 0
        self._load_task: asyncio.Task[None] | None = None
        self._video_cache_task: asyncio.Task[None] | None = None
        self._active_video_cache_id: str | None = None
        self._queued_video_cache_ids: list[str] = []
        self._prune_task: asyncio.Task[None] | None = None
        self._pending_prune_assets: list[dict[str, Any]] | None = None
        self._is_removed = False
        self._legacy_jpegs_cleaned = False

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
        await self.hass.async_add_executor_job(
            self._prune_video_cache_budget, self._protected_video_ids()
        )

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
        self._rotation_interval = self._config_entry.options.get(
            CONF_ROTATION_INTERVAL,
            self._config_entry.data.get(CONF_ROTATION_INTERVAL, DEFAULT_ROTATION_INTERVAL),
        )
        self._next_rotation_at = time.time() + self._rotation_interval
        self._rotation_unsubscribe = async_track_time_interval(
            self.hass,
            self._async_rotate,
            timedelta(seconds=self._rotation_interval),
        )

        # Load the first image
        await self._schedule_current_media_load()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the rotation timer."""
        self._is_removed = True
        if self._rotation_unsubscribe is not None:
            self._rotation_unsubscribe()
            self._rotation_unsubscribe = None

        tasks = [
            task
            for task in [self._load_task, self._video_cache_task, self._prune_task]
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._queued_video_cache_ids.clear()

        if self._cache_dir is not None:
            await self.hass.async_add_executor_job(self._remove_partial_cache_files)

        cameras = self.hass.data.get(DOMAIN, {}).get(DATA_CAMERAS, {})
        if cameras.get(self._config_entry.entry_id) is self:
            cameras.pop(self._config_entry.entry_id)
        await super().async_will_remove_from_hass()

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

    @property
    def media_revision(self) -> int:
        """Return the revision of the media currently displayed."""
        return self._media_revision

    @property
    def next_rotation_at(self) -> float | None:
        """Return the next planned rotation as a Unix timestamp."""
        return self._next_rotation_at

    def has_video_asset(self, asset_id: str) -> bool:
        """Return whether an asset is a video in the current media pool."""
        return any(
            asset.get("id") == asset_id and asset.get("type") == ASSET_TYPE_VIDEO
            for asset in self._coordinator.data or []
        )

    @property
    def player_token(self) -> str:
        """Return a stable opaque token for the embedded player URL."""
        return create_player_token(
            self._coordinator.api_key,
            self._config_entry.entry_id,
            self._config_entry.data.get(CONF_PLAYER_TOKEN_SALT, ""),
        )

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

        # A fresh pool can change the current asset at the same index. Cancel
        # any older fetch so a slow response cannot replace the new media.
        self._schedule_current_media_load()

        self._cancel_unavailable_video_cache(assets)

        # Coalesce cache pruning so a stale asset pool cannot delete files
        # retained by a newer coordinator refresh.
        if self._cache_dir is not None:
            self._schedule_cache_prune(assets)

        self.async_write_ha_state()

    async def _async_rotate(self, _now=None) -> None:
        """Advance to the next photo in the list."""
        self._next_rotation_at = time.time() + self._rotation_interval
        assets = self._coordinator.data
        if not assets:
            return

        self._current_index = (self._current_index + 1) % len(assets)
        try:
            await self._schedule_current_media_load()
        except asyncio.CancelledError:
            # A coordinator update has already scheduled a newer media load.
            if self._is_removed:
                raise
        self.async_write_ha_state()

    def _schedule_current_media_load(self) -> asyncio.Task[None]:
        """Start loading the selected media, superseding any older request."""
        self._media_revision += 1
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()

        asset = self.current_asset
        if asset is None or self._is_removed:
            self._load_task = self.hass.async_create_task(self._load_current_image(None, 0))
            return self._load_task

        self._load_task = self.hass.async_create_task(
            self._load_current_image(asset, self._media_revision)
        )
        return self._load_task

    @staticmethod
    def _asset_cache_id(asset: dict[str, Any]) -> str | None:
        """Return the cache filename stem for a single slide."""
        asset_id = asset.get("id")
        if not asset_id:
            return None
        return str(asset_id)

    def _cached_image_path(self, asset: dict[str, Any]) -> pathlib.Path | None:
        """Return the JPEG cache path for a slide, if caching is available."""
        if self._cache_dir is None:
            return None
        if cache_id := self._asset_cache_id(asset):
            return self._cache_dir / f"{cache_id}.jpg"
        return None

    def _schedule_cache_prune(self, assets: list[dict[str, Any]]) -> None:
        """Coalesce cache cleanup requests to the newest asset pool."""
        self._pending_prune_assets = assets
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = self.hass.async_create_task(self._async_prune_cache())

    async def _async_prune_cache(self) -> None:
        """Serialize cache cleanup, keeping only the latest coordinator data."""
        while not self._is_removed and self._pending_prune_assets is not None:
            assets = self._pending_prune_assets
            self._pending_prune_assets = None
            await self._prune_cache(assets)
            await self.hass.async_add_executor_job(
                self._prune_video_cache_budget, self._protected_video_ids()
            )

    async def _prune_cache(self, assets: list[dict[str, Any]]) -> None:
        """Prune completed media files outside the current asset pool."""
        image_ids = {
            cache_id
            for asset in assets
            if (cache_id := self._asset_cache_id(asset)) is not None
        }
        video_ids = {
            str(asset["id"])
            for asset in assets
            if asset.get("type") == ASSET_TYPE_VIDEO and asset.get("id")
        }

        def _delete_excess() -> None:
            for f in self._cache_dir.glob("*.jpg"):
                try:
                    # Numeric JPEGs are the legacy index cache. They remain
                    # available for startup fallback until a new cache write.
                    if not f.stem.isdecimal() and f.stem not in image_ids:
                        f.unlink()
                except OSError:
                    pass

            for f in self._cache_dir.glob("*.mp4"):
                try:
                    if f.stem not in video_ids:
                        f.unlink()
                except OSError:
                    pass

        await self.hass.async_add_executor_job(_delete_excess)

    def _remove_partial_cache_files(self) -> None:
        """Remove interrupted cache writes after all worker tasks stop."""
        if self._cache_dir is None:
            return
        for pattern in ("*.jpg.part", "*.jpg.*.part", "*.mp4.part"):
            for cache_file in self._cache_dir.glob(pattern):
                with contextlib.suppress(OSError):
                    cache_file.unlink()

    @staticmethod
    def _atomic_write(path: pathlib.Path, data: bytes) -> None:
        """Write bytes atomically so readers never observe a partial file."""
        temp_file = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
        temp_file.write_bytes(data)
        temp_file.replace(path)

    async def _write_image_cache(self, cache_file: pathlib.Path, data: bytes) -> None:
        """Atomically save a thumbnail and retire legacy cache slots once."""
        await self.hass.async_add_executor_job(self._atomic_write, cache_file, data)
        if not self._legacy_jpegs_cleaned:
            await self.hass.async_add_executor_job(self._remove_legacy_jpegs)
            self._legacy_jpegs_cleaned = True

    def _remove_legacy_jpegs(self) -> None:
        """Remove numbered JPEG caches after a successful cache migration."""
        if self._cache_dir is None:
            return
        for cache_file in self._cache_dir.glob("*.jpg"):
            if cache_file.stem.isdecimal():
                with contextlib.suppress(OSError):
                    cache_file.unlink()

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

    async def async_cached_video_path(self, asset_id: str) -> pathlib.Path | None:
        """Return and mark a cached video as recently used."""
        cache_file = self.cached_video_path(asset_id)
        if cache_file is not None:
            with contextlib.suppress(OSError):
                await self.hass.async_add_executor_job(cache_file.touch)
        return cache_file

    def _schedule_video_cache(self, asset_id: str) -> None:
        """Queue a short video cache download without unbounded concurrency."""
        if self.cached_video_path(asset_id) is not None:
            return
        if asset_id == self._active_video_cache_id or asset_id in self._queued_video_cache_ids:
            return
        self._queued_video_cache_ids.insert(0, asset_id)
        self._start_next_video_cache()

    def _start_next_video_cache(self) -> None:
        """Start at most one queued download for this config entry."""
        if self._is_removed or (
            self._video_cache_task is not None and not self._video_cache_task.done()
        ):
            return

        while self._queued_video_cache_ids:
            asset_id = self._queued_video_cache_ids.pop(0)
            if not self.has_video_asset(asset_id) or self.cached_video_path(asset_id):
                continue
            self._active_video_cache_id = asset_id
            self._video_cache_task = self.hass.async_create_task(
                self._async_cache_video(asset_id)
            )
            return
        self._active_video_cache_id = None

    def _cancel_unavailable_video_cache(self, assets: list[dict[str, Any]]) -> None:
        """Cancel queued or active downloads no longer present in the pool."""
        video_ids = {
            str(asset["id"])
            for asset in assets
            if asset.get("type") == ASSET_TYPE_VIDEO and asset.get("id")
        }
        self._queued_video_cache_ids = [
            asset_id for asset_id in self._queued_video_cache_ids if asset_id in video_ids
        ]
        if (
            self._active_video_cache_id not in video_ids
            and self._video_cache_task is not None
            and not self._video_cache_task.done()
        ):
            self._video_cache_task.cancel()

    def _prune_video_cache_budget(self, protected_ids: set[str]) -> None:
        """Evict least-recently-used videos until the per-entry budget fits."""
        if self._cache_dir is None:
            return
        protected_names = {f"{asset_id}.mp4" for asset_id in protected_ids}
        for cache_file in video_files_to_evict(
            self._cache_dir.glob("*.mp4"),
            MAX_VIDEO_CACHE_TOTAL_BYTES,
            protected_names,
        ):
            with contextlib.suppress(OSError):
                cache_file.unlink()

    def _protected_video_ids(self) -> set[str]:
        """Return video IDs that must not be evicted during this operation."""
        protected_ids = {
            asset_id
            for asset_id in [self._active_video_cache_id]
            if asset_id is not None
        }
        current_asset = self.current_asset
        if current_asset and current_asset.get("type") == ASSET_TYPE_VIDEO:
            asset_id = current_asset.get("id")
            if asset_id:
                protected_ids.add(str(asset_id))
        return protected_ids

    async def _async_cache_video(self, asset_id: str) -> None:
        """Cache a video only when Immich reports it is below the size limit."""
        if self._cache_dir is None:
            return

        cache_file = self._cache_dir / f"{asset_id}.mp4"
        temp_file = self._cache_dir / f"{asset_id}.mp4.part"
        session = async_get_clientsession(self.hass)
        url = f"{self._coordinator.host}/api/assets/{asset_id}/video/playback"
        headers = {"x-api-key": self._coordinator.api_key, "Range": "bytes=0-0"}
        completed = False
        file_handle = None

        try:
            async with session.get(url, headers=headers, timeout=15) as resp:
                content_range = resp.headers.get("Content-Range", "")
                total_text = content_range.rsplit("/", 1)[-1]
                if resp.status != 206 or not total_text.isdecimal():
                    return
                expected_size = int(total_text)
                if expected_size >= MAX_VIDEO_CACHE_BYTES:
                    return

            async with session.get(
                url,
                headers={"x-api-key": self._coordinator.api_key},
                timeout=ClientTimeout(total=None, sock_connect=15),
            ) as resp:
                if resp.status != 200:
                    return
                content_length = resp.content_length
                if content_length is not None and content_length >= MAX_VIDEO_CACHE_BYTES:
                    return

                await self.hass.async_add_executor_job(temp_file.unlink, True)
                file_handle = await self.hass.async_add_executor_job(
                    temp_file.open, "xb"
                )
                downloaded = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded >= MAX_VIDEO_CACHE_BYTES:
                        return
                    await self.hass.async_add_executor_job(file_handle.write, chunk)

            if downloaded != expected_size:
                return

            if self._is_removed or not self.has_video_asset(asset_id):
                return

            await self.hass.async_add_executor_job(file_handle.close)
            file_handle = None
            await self.hass.async_add_executor_job(temp_file.replace, cache_file)
            completed = True
            protected_ids = self._protected_video_ids() | {asset_id}
            await self.hass.async_add_executor_job(
                self._prune_video_cache_budget, protected_ids
            )
            _LOGGER.debug("Cached Immich video %s", asset_id)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Could not cache Immich video %s: %s", asset_id, err)
        finally:
            if file_handle is not None:
                with contextlib.suppress(OSError):
                    await self.hass.async_add_executor_job(file_handle.close)
            if not completed:
                with contextlib.suppress(FileNotFoundError, OSError):
                    await self.hass.async_add_executor_job(temp_file.unlink)
            if self._active_video_cache_id == asset_id:
                self._active_video_cache_id = None
            self._video_cache_task = None
            self._start_next_video_cache()

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

    async def _load_current_image(
        self, asset: dict[str, Any] | None, revision: int
    ) -> None:
        """Fetch image bytes for the current asset from Immich.

        On success the bytes are written to disk so they can be served if
        Immich is unavailable during a later rotation cycle.  On any failure
        the cached file for this asset is used instead; if no cache exists the
        last in-memory image is preserved unchanged.
        """
        if asset is None or revision == 0:
            return

        asset_id = asset.get("id")
        if not asset_id:
            return

        cache_file = self._cached_image_path(asset)

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
                    if revision != self._media_revision or self._is_removed:
                        return
                    self._current_image_bytes = data
                    if cache_file is not None:
                        await self._write_image_cache(cache_file, data)
                else:
                    _LOGGER.warning(
                        "Could not fetch both portrait thumbnails for pair %s",
                        asset_id,
                    )
                    await self._serve_from_cache(cache_file, revision)
            else:
                # A single image or video thumbnail
                data = await self._fetch_single_thumbnail(asset_id)
                if data:
                    if revision != self._media_revision or self._is_removed:
                        return
                    self._current_image_bytes = data
                    if cache_file is not None:
                        await self._write_image_cache(cache_file, data)
                    if asset.get("type") == ASSET_TYPE_VIDEO:
                        self._schedule_video_cache(asset_id)
                else:
                    await self._serve_from_cache(cache_file, revision)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("Error fetching Immich thumbnail for %s: %s", asset_id, err)
            await self._serve_from_cache(cache_file, revision)

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
        """Return the most recently written JPEG cache file, if any exists."""
        if self._cache_dir is None:
            return None

        candidates: list[pathlib.Path] = []
        for cache_file in self._cache_dir.glob("*.jpg"):
            try:
                cache_file.stat()
                candidates.append(cache_file)
            except OSError:
                continue

        if not candidates:
            return None

        return max(candidates, key=lambda cache_file: cache_file.stat().st_mtime)

    async def _serve_from_cache(
        self, cache_file: pathlib.Path | None, revision: int | None = None
    ) -> None:
        """Load image bytes from the on-disk cache for this asset, if available."""
        if cache_file is None:
            return
        try:
            data = await self.hass.async_add_executor_job(cache_file.read_bytes)
            if revision is not None and (
                revision != self._media_revision or self._is_removed
            ):
                return
            self._current_image_bytes = data
            _LOGGER.debug("Serving cached image: %s", cache_file.name)
        except FileNotFoundError:
            pass  # No cache yet for this asset; keep the last in-memory image
        except OSError as err:
            _LOGGER.debug("Could not read cache file %s: %s", cache_file, err)
