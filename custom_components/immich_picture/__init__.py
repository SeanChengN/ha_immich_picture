"""Immich integration for Home Assistant."""

from __future__ import annotations

import logging
import pathlib
import secrets
import shutil

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CONF_PLAYER_TOKEN_SALT,
    DATA_CAMERAS,
    DOMAIN,
    SERVICE_ROTATE_PLAYER_TOKEN,
)
from .coordinator import ImmichDataUpdateCoordinator
from .player import (
    ImmichPlayerPageView,
    ImmichPlayerStateView,
    ImmichVideoPlaybackView,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera", "sensor"]


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up shared HTTP views for the Immich player."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(DATA_CAMERAS, {})
    hass.http.register_view(ImmichPlayerPageView(hass))
    hass.http.register_view(ImmichPlayerStateView(hass))
    hass.http.register_view(ImmichVideoPlaybackView(hass))
    hass.services.async_register(
        DOMAIN,
        SERVICE_ROTATE_PLAYER_TOKEN,
        lambda call: _async_rotate_player_token(hass, call.data.get("entry_id")),
        schema=vol.Schema({vol.Optional("entry_id"): str}),
    )
    return True


async def _async_rotate_player_token(
    hass: HomeAssistant, entry_id: str | None
) -> None:
    """Rotate one or all player capability URLs."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id is not None:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ValueError(f"No {DOMAIN} config entry exists with id {entry_id}")

    for entry in entries:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_PLAYER_TOKEN_SALT: secrets.token_urlsafe(32)},
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Immich from a config entry."""
    coordinator = ImmichDataUpdateCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        _LOGGER.warning(
            "Initial refresh failed for %s; continuing with cached image fallback: %s",
            entry.title,
            err,
        )

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(DATA_CAMERAS, {})
    domain_data[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload entry when options are updated
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Immich config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all on-disk media cache for a deleted config entry."""
    cache_root = pathlib.Path(hass.config.path(DOMAIN, "image_cache"))
    cache_dir = cache_root / entry.entry_id

    def _remove_cache() -> None:
        if cache_dir.parent.resolve() != cache_root.resolve():
            raise ValueError("Refusing to remove a cache directory outside its root")
        shutil.rmtree(cache_dir, ignore_errors=True)

    await hass.async_add_executor_job(_remove_cache)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
