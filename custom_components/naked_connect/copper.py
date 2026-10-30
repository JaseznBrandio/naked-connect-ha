"""Copper ioniser controls for Naked freshwater systems.

Created only when the unit reports a copper output setting (ION_SET). The
commands match what the Naked Connect app sends:
- copper output: write ION_SET (the unit doesn't echo it back)
- start copper boost: write COPPER_LEVEL 0; the unit then runs at 100% for 27 hours
- stop copper boost: write ION_Status OFF
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.exceptions import HomeAssistantError

from .coordinator import NakedCoordinator
from .entity import NakedEntity

COPPER_MARKER = "ION_SET"


def has_copper(coordinator: NakedCoordinator) -> bool:
    return coordinator.has(COPPER_MARKER)


def _on(props: dict[str, Any], key: str) -> bool | None:
    value = (props.get(key) or {}).get("value")
    if value is None:
        return None
    return str(value).upper() in ("ON", "1", "TRUE")


def copper_boost_ends(props: dict[str, Any]) -> datetime | None:
    """When the copper boost finishes: minutes left, counted from when the unit said so."""
    if not _on(props, "ION_Status"):
        return None
    item = props.get("ION_TimeRemain") or {}
    try:
        minutes = float(item.get("numberValue", item.get("value")))
        ts = float(item.get("timestamp") or time.time() * 1000)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return datetime.fromtimestamp(ts / 1000 + minutes * 60, tz=timezone.utc)


class _CopperEntity(NakedEntity):
    @property
    def _props(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("properties") or {}


# ------------------------------------------------------------------ sensors


class CopperLevel(_CopperEntity, SensorEntity):
    _attr_name = "Copper level"
    _attr_icon = "mdi:flask-outline"
    _attr_native_unit_of_measurement = "ppm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "copper_level")

    @property
    def native_value(self) -> float | None:
        item = self._props.get("COPPER_LEVEL") or {}
        try:
            return float(item.get("numberValue", item.get("value")))
        except (TypeError, ValueError):
            return None


class CopperBoostEnds(_CopperEntity, SensorEntity):
    _attr_name = "Copper boost ends"
    _attr_icon = "mdi:rocket-launch-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "copper_boost_ends")

    @property
    def native_value(self) -> datetime | None:
        return copper_boost_ends(self._props)


class CopperRunHours(_CopperEntity, SensorEntity):
    _attr_name = "Copper ioniser run time"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "copper_run_hours")

    @property
    def native_value(self) -> float | None:
        item = self._props.get("IONRunTime") or {}
        try:
            return float(item.get("numberValue", item.get("value")))
        except (TypeError, ValueError):
            return None


# ----------------------------------------------------------- binary sensors


class CopperFlag(_CopperEntity, BinarySensorEntity):
    def __init__(self, coordinator: NakedCoordinator, key: str, prop: str, name: str,
                 icon: str, device_class=None, diagnostic: bool = False) -> None:
        super().__init__(coordinator, key)
        self._prop_key = prop
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_class = device_class
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool | None:
        return _on(self._props, self._prop_key)


# ------------------------------------------------------------------- number


class CopperOutput(_CopperEntity, NumberEntity):
    """Copper output. During a copper boost the unit runs at 100% and this is locked."""

    _attr_name = "Copper output"
    _attr_icon = "mdi:flask-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "copper_output")

    @property
    def native_value(self) -> float | None:
        item = self._props.get("ION_SET") or {}
        try:
            return float(item.get("numberValue", item.get("value")))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        if _on(self._props, "ION_Status"):
            raise HomeAssistantError("Stop the copper boost before changing the copper output.")
        await self.coordinator.async_write({"ION_SET": int(round(value))})


# ------------------------------------------------------------------ buttons


class CopperButton(_CopperEntity, ButtonEntity):
    def __init__(self, coordinator: NakedCoordinator, start: bool) -> None:
        super().__init__(coordinator, "copper_boost_start" if start else "copper_boost_stop")
        self._start = start
        self._attr_name = "Start copper boost" if start else "Stop copper boost"
        self._attr_icon = "mdi:rocket-launch" if start else "mdi:stop-circle-outline"

    async def async_press(self) -> None:
        if self._start:
            # Writing COPPER_LEVEL 0 is the app's boost trigger, not a reading.
            await self.coordinator.async_write(
                {"COPPER_LEVEL": 0}, show={"ION_Status": "ON", "ION_SET": 100}, hold=False
            )
        else:
            # The unit goes back to the normal copper output and reports it.
            self.coordinator.forget_pending("ION_SET")
            await self.coordinator.async_write(
                {"ION_Status": "OFF"}, show={"ION_TimeRemain": 0}
            )


# ---------------------------------------------------------------- factories


def copper_sensors(c: NakedCoordinator) -> list[SensorEntity]:
    if not has_copper(c):
        return []
    ents: list[SensorEntity] = [CopperBoostEnds(c)]
    if c.has("COPPER_LEVEL"):
        ents.insert(0, CopperLevel(c))
    if c.has("IONRunTime"):
        ents.append(CopperRunHours(c))
    return ents


def copper_binary_sensors(c: NakedCoordinator) -> list[BinarySensorEntity]:
    if not has_copper(c):
        return []
    ents: list[BinarySensorEntity] = [
        CopperFlag(c, "copper_boost_active", "ION_Status", "Copper boost", "mdi:rocket-launch"),
    ]
    if c.has("ION_RunStatus"):
        ents.insert(0, CopperFlag(c, "copper_running", "ION_RunStatus", "Copper ioniser",
                                  "mdi:flash", BinarySensorDeviceClass.RUNNING))
    if c.has("IONLowTempFlag"):
        ents.append(CopperFlag(c, "copper_low_temp", "IONLowTempFlag",
                               "Copper low temperature hold", "mdi:thermometer-low",
                               diagnostic=True))
    if c.has("ION_InitStartUp"):
        ents.append(CopperFlag(c, "copper_start_up", "ION_InitStartUp",
                               "Copper start-up", "mdi:restart", diagnostic=True))
    return ents


def copper_numbers(c: NakedCoordinator) -> list[NumberEntity]:
    return [CopperOutput(c)] if has_copper(c) else []


def copper_buttons(c: NakedCoordinator) -> list[ButtonEntity]:
    return [CopperButton(c, True), CopperButton(c, False)] if has_copper(c) else []
