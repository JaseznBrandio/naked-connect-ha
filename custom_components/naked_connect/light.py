"""Light platform: pool and spa lights on the controller's sockets, with colours.

A socket is a light when the hub reports a light type for it (the "light type"
picked in the Naked Connect app). Light types with a known colour list get the
colours and shows as effects; others get on/off.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LIGHT_PROFILES, LIGHT_TYPE_NAMES
from .coordinator import NakedCoordinator
from .entity import NakedSocketEntity, socket_display_name, socket_is_on


def light_type(fields: dict[str, str]) -> str | None:
    """The socket's light type, or None when it isn't a light."""
    kind = str(fields.get("manufacturer", "")).upper()
    if not kind or kind in ("SINGLE_COLOUR", "NONE", "NULL"):
        return None
    return kind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NakedCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [
            s for s in sorted(coordinator.sockets)
            if s not in known and light_type(coordinator.sockets[s].get("fields") or {})
        ]
        if new:
            known.update(new)
            async_add_entities(NakedLight(coordinator, s) for s in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class NakedLight(NakedSocketEntity, LightEntity):
    """On / off, plus the light's own colours and shows as effects."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_icon = "mdi:lightbulb"

    def __init__(self, coordinator: NakedCoordinator, socket: str) -> None:
        super().__init__(coordinator, socket, "light")
        self._attr_name = socket_display_name(coordinator, socket)

    @property
    def _profile(self) -> dict[str, str]:
        return LIGHT_PROFILES.get(light_type(self._fields) or "", {})

    @property
    def supported_features(self) -> LightEntityFeature:
        return LightEntityFeature.EFFECT if self._profile else LightEntityFeature(0)

    @property
    def effect_list(self) -> list[str] | None:
        return list(self._profile.values()) or None

    @property
    def effect(self) -> str | None:
        return self._profile.get(str(self._fields.get("color", "")).upper())

    @property
    def is_on(self) -> bool | None:
        return socket_is_on(self._fields)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        kind = light_type(self._fields)
        attrs["light_type"] = LIGHT_TYPE_NAMES.get(kind or "", kind)
        attrs["colour_control"] = bool(self._profile)
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None and self._profile:
            code = next((c for c, name in self._profile.items() if name == effect), None)
            if code is None:
                raise ValueError(f"Unknown colour or show: {effect}")
            await self.coordinator.async_set_light_color(self._socket, code)
            return
        await self.coordinator.async_set_socket(self._socket, "ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_socket(self._socket, "OFF")
