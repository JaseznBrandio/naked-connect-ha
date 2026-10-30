"""Button platform: chlorinator boost, salt test and copper boost."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .chlorinator import chlor_buttons
from .copper import copper_buttons
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([*chlor_buttons(c), *copper_buttons(c)])
