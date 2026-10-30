"""Switch platform: one switch per socket (lights, pumps, blowers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .chlorinator import chlor_switches
from .coordinator import NakedCoordinator
from .entity import NakedSocketEntity, socket_display_name, socket_is_on

ICONS = {
    "light": "mdi:lightbulb",
    "blower": "mdi:weather-windy",
    "pump": "mdi:pump",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NakedCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(chlor_switches(coordinator))
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [s for s in sorted(coordinator.sockets) if s not in known]
        if new:
            known.update(new)
            async_add_entities(NakedSocketSwitch(coordinator, s) for s in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class NakedSocketSwitch(NakedSocketEntity, SwitchEntity):
    """On = force on (SW=ON). Off = force off (SW=OFF). Use the mode select for Auto."""

    def __init__(self, coordinator: NakedCoordinator, socket: str) -> None:
        super().__init__(coordinator, socket, "switch")
        self._attr_name = socket_display_name(coordinator, socket)
        lowered = self._attr_name.lower()
        self._attr_icon = next(
            (icon for word, icon in ICONS.items() if word in lowered), "mdi:power-socket-au"
        )

    @property
    def is_on(self) -> bool | None:
        return socket_is_on(self._fields)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_socket(self._socket, "ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_socket(self._socket, "OFF")
