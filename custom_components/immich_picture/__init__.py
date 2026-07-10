"""Immich integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DATA_CAMERAS, DOMAIN
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
    return True


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


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
