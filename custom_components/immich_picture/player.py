"""Authenticated HTTP views for the Immich media player."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import ClientTimeout, hdrs, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ASSET_TYPE_VIDEO, DATA_CAMERAS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_VIDEO_RESPONSE_HEADERS = (
    hdrs.ACCEPT_RANGES,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_RANGE,
    hdrs.CONTENT_TYPE,
    hdrs.ETAG,
    hdrs.LAST_MODIFIED,
)

_PLAYER_SECURITY_HEADERS = {
    hdrs.CACHE_CONTROL: "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; connect-src 'self'; img-src 'self'; "
        "media-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    ),
}

_MEDIA_SECURITY_HEADERS = {
    hdrs.CACHE_CONTROL: "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class _ImmichPlayerView(HomeAssistantView):
    """Base class for views that need an active Immich camera."""

    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the view with Home Assistant state."""
        self.hass = hass

    def _get_camera(self, request: web.Request, entry_id: str) -> Any:
        """Return the camera registered for an active config entry."""
        cameras = self.hass.data.get(DOMAIN, {}).get(DATA_CAMERAS, {})
        camera = cameras.get(entry_id)
        if camera is None:
            raise web.HTTPNotFound
        if not camera.is_valid_player_token(request.query.get("token")):
            raise web.HTTPForbidden
        return camera


class ImmichPlayerPageView(_ImmichPlayerView):
    """Serve the built-in HTML5 media player."""

    url = "/api/immich_picture/player/{entry_id}"
    name = "api:immich_picture:player"

    async def get(self, request: web.Request, entry_id: str) -> web.FileResponse:
        """Return the player page for an active config entry."""
        self._get_camera(request, entry_id)
        response = web.FileResponse(Path(__file__).with_name("player.html"))
        response.headers.update(_PLAYER_SECURITY_HEADERS)
        return response


class ImmichPlayerStateView(_ImmichPlayerView):
    """Return the current slideshow media for the player page."""

    url = "/api/immich_picture/player/{entry_id}/state"
    name = "api:immich_picture:player_state"

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        """Return media state without exposing Immich credentials."""
        camera = self._get_camera(request, entry_id)
        asset = camera.current_asset
        if asset is None:
            return web.json_response({"revision": None})

        asset_id = asset.get("id")
        asset_type = asset.get("type")
        revision = f"{camera.media_revision}:{asset_id}"
        snapshot_url = (
            f"/api/camera_proxy/{camera.entity_id}?token={camera.access_tokens[-1]}"
            f"&v={quote(revision)}"
        )

        video_url = None
        if asset_id and asset_type == ASSET_TYPE_VIDEO:
            video_url = (
                f"/api/{DOMAIN}/player/{entry_id}/video/{quote(str(asset_id), safe='')}"
                f"?token={quote(request.query['token'], safe='')}"
            )

        return web.json_response(
            {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "revision": revision,
                "next_rotation_at": camera.next_rotation_at,
                "snapshot_url": snapshot_url,
                "video_url": video_url,
            },
            headers=_MEDIA_SECURITY_HEADERS,
        )


class ImmichVideoPlaybackView(_ImmichPlayerView):
    """Proxy an Immich video playback request for the embedded player."""

    url = "/api/immich_picture/player/{entry_id}/video/{asset_id}"
    name = "api:immich_picture:video"

    async def get(
        self, request: web.Request, entry_id: str, asset_id: str
    ) -> web.StreamResponse:
        """Relay an authenticated video response, including byte ranges."""
        camera = self._get_camera(request, entry_id)
        if not camera.has_video_asset(asset_id):
            raise web.HTTPNotFound

        if cache_file := await camera.async_cached_video_path(asset_id):
            response = web.FileResponse(cache_file)
            response.headers.update(_MEDIA_SECURITY_HEADERS)
            return response

        upstream_headers = {"x-api-key": camera._coordinator.api_key}
        for header in (hdrs.RANGE, hdrs.IF_RANGE):
            if value := request.headers.get(header):
                upstream_headers[header] = value

        url = f"{camera._coordinator.host}/api/assets/{asset_id}/video/playback"
        session = async_get_clientsession(self.hass)

        try:
            async with session.get(
                url,
                headers=upstream_headers,
                timeout=ClientTimeout(total=None, sock_connect=15),
            ) as upstream:
                if upstream.status not in (200, 206):
                    _LOGGER.warning(
                        "Immich returned HTTP %s for video playback asset %s",
                        upstream.status,
                        asset_id,
                    )
                    raise web.HTTPBadGateway

                response_headers = {
                    header: value
                    for header in _VIDEO_RESPONSE_HEADERS
                    if (value := upstream.headers.get(header)) is not None
                }
                response_headers.update(_MEDIA_SECURITY_HEADERS)
                response = web.StreamResponse(
                    status=upstream.status,
                    headers=response_headers,
                )
                await response.prepare(request)

                try:
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                except ConnectionResetError:
                    _LOGGER.debug("Video player disconnected for asset %s", asset_id)
                finally:
                    try:
                        await response.write_eof()
                    except ConnectionResetError:
                        pass

                return response
        except asyncio.CancelledError:
            raise
        except web.HTTPException:
            raise
        except Exception as err:
            _LOGGER.warning("Could not proxy Immich video asset %s: %s", asset_id, err)
            raise web.HTTPBadGateway from err
