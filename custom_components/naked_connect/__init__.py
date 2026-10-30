"""The Naked Connect integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NakedClient
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SHOW_PANEL,
    CONF_USERNAME,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OLD_DEFAULT_SCAN_INTERVAL,
    PLATFORMS,
)
from .coordinator import NakedCoordinator
from .push import NakedPush
from .panel import async_maybe_remove_panel, async_setup_panel


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move entries still on the old 5 minute default to the new 30 second one."""
    if entry.version == 1 and entry.minor_version < 2:
        options = dict(entry.options)
        if options.get(CONF_SCAN_INTERVAL, OLD_DEFAULT_SCAN_INTERVAL) == OLD_DEFAULT_SCAN_INTERVAL:
            options[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Naked Connect from a config entry."""
    data = entry.data
    session = async_get_clientsession(hass)

    client = NakedClient(
        session=session,
        host=data.get(CONF_HOST, DEFAULT_HOST),
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        device_id=data[CONF_DEVICE_ID],
    )

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    _remove_replaced_entities(hass, entry, client.device_id)

    coordinator = NakedCoordinator(hass, entry, client, scan_interval)
    await coordinator.async_load_cache()
    await coordinator.async_load_state()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    push = NakedPush(session, client, coordinator.handle_push, coordinator.push_state)
    push.start(lambda coro, name: entry.async_create_background_task(hass, coro, name))
    entry.async_on_unload(push.stop)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if entry.options.get(CONF_SHOW_PANEL, True):
        await async_setup_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        async_maybe_remove_panel(hass, entry.entry_id)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options (e.g. scan interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


# Entities from earlier versions that have been replaced:
# winter/spa switches -> Work mode select; internal_temperature -> power_pack_temp;
# pH dose setting -> pH2 prime amount + Start pH dose.
_REPLACED = ("WinterMode_ctl", "SpaMode_ctl", "internal_temperature", "pHRunAtDose_ctl")


def _remove_replaced_entities(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> None:
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id in {f"{device_id}_{key}" for key in _REPLACED}:
            registry.async_remove(ent.entity_id)
