"""Binary sensor platform for Naked Connect (on/off states)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NakedCoordinator
from .copper import copper_binary_sensors
from .entity import NakedEntity


@dataclass(frozen=True, kw_only=True)
class NakedBinaryDescription(BinarySensorEntityDescription):
    """Binary sensor description; on_value is the reported value meaning 'on'."""

    on_value: str = "ON"


BINARY_SENSORS: tuple[NakedBinaryDescription, ...] = (
    NakedBinaryDescription(
        key="PumpStatus",
        name="Chlorinator pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:pump",
    ),
    NakedBinaryDescription(
        key="pHStatus",
        name="pH dosing pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:pump",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NakedCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        *(NakedBinarySensor(coordinator, desc) for desc in BINARY_SENSORS),
        *copper_binary_sensors(coordinator),
    ])


class NakedBinarySensor(NakedEntity, BinarySensorEntity):
    entity_description: NakedBinaryDescription

    def __init__(
        self, coordinator: NakedCoordinator, description: NakedBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self._prop_value(self.entity_description.key)
        if value is None:
            return None
        return str(value).upper() == self.entity_description.on_value.upper()
