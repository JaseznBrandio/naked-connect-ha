"""Select platform: On / Off / Auto mode per socket."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .chlorinator import chlor_selects
from .coordinator import NakedCoordinator
from .entity import NakedSocketEntity, socket_display_name

OPTIONS = {"On": "ON", "Off": "OFF", "Auto": "AUTO"}
REVERSE = {v: k for k, v in OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NakedCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(chlor_selects(coordinator))
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [s for s in sorted(coordinator.sockets) if s not in known]
        if new:
            known.update(new)
            async_add_entities(NakedSocketMode(coordinator, s) for s in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class NakedSocketMode(NakedSocketEntity, SelectEntity):
    """Auto hands the socket back to its built-in timer."""

    _attr_options = list(OPTIONS)
    _attr_icon = "mdi:timer-cog-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: NakedCoordinator, socket: str) -> None:
        super().__init__(coordinator, socket, "mode")
        self._attr_name = f"{socket_display_name(coordinator, socket)} mode"

    @property
    def current_option(self) -> str | None:
        return REVERSE.get(str(self._fields.get("SW", "")).upper())

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_socket(self._socket, OPTIONS[option])
