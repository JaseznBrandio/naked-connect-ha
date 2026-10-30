"""Sensor platform for Naked Connect."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, fault_text
from .coordinator import NakedCoordinator
from .copper import copper_sensors
from .salt_test import condition_text
from .entity import NakedEntity


@dataclass(frozen=True, kw_only=True)
class NakedSensorDescription(SensorEntityDescription):
    """Sensor description with an optional divisor for scaled integers."""

    # The device reports some values multiplied by 10 (e.g. WaterTemp 368 = 36.8).
    divide: float = 1.0
    # "number" uses numberValue (numeric props); "text" uses value (enum/string).
    source: str = "number"
    # Optional friendly names for reported codes, e.g. {"NULL": "Normal"}.
    value_map: dict[str, str] | None = None
    # Optional computed value from all properties (used for timer windows).
    value_fn: Callable[[dict[str, Any]], Any] | None = None
    # Only create the entity when the device reports this property.
    requires: str | None = None
    # Property to read when it differs from the key (key also forms the unique id).
    prop: str | None = None


def _epoch(value):
    if not isinstance(value, (int, float)):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc)


TEMP_LABELS = {"WaterTemp": "TempNameA", "WaterTempB": "TempNameB"}

SENSORS: tuple[NakedSensorDescription, ...] = (
    # --- Primary readings ---
    NakedSensorDescription(
        key="WaterTemp",
        name="Water temperature A",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        divide=10.0,
    ),
    NakedSensorDescription(
        key="WaterTempB",
        name="Water temperature B",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        divide=10.0,
    ),
    NakedSensorDescription(
        key="SaltLevel",
        name="Salt level",
        native_unit_of_measurement="ppm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:shaker-outline",
    ),
    NakedSensorDescription(
        key="ActualOutput",
        name="Actual output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
    ),
    NakedSensorDescription(
        key="OutputSetPoint",
        name="Output setpoint",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:target",
    ),
    NakedSensorDescription(
        key="ChlorineProduction",
        name="Chlorine production",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flask-outline",
    ),
    # --- Modes / status (text) ---
    NakedSensorDescription(
        key="WorkMode", name="Work mode", source="text", icon="mdi:cog-outline",
        value_map={"NULL": "Normal", "SPA": "Spa", "WINTER": "Winter", "BOOST": "Boost",
                   "BACKWASH": "Backwash", "SALT_TEST": "Salt test", "SALT_ADD": "Salt add"}
    ),
    NakedSensorDescription(
        key="PowerMode", name="Power mode", source="text", icon="mdi:power"
    ),
    NakedSensorDescription(
        key="CellStatus", name="Cell status", source="text", icon="mdi:flash-outline"
    ),
    NakedSensorDescription(
        key="CellDirection",
        name="Cell direction",
        source="text",
        icon="mdi:swap-horizontal",
        entity_registry_enabled_default=False,
    ),
    # --- Diagnostics ---
    NakedSensorDescription(
        key="WIFI_RSSI",
        name="WiFi signal",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
        entity_registry_enabled_default=False,
    ),
    NakedSensorDescription(
        key="Fault",
        name="Fault code",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
    ),
    NakedSensorDescription(
        key="PumpRunningHours",
        name="Pump running hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    NakedSensorDescription(
        key="CellRunningHours",
        name="Cell running hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    NakedSensorDescription(
        key="PowerOnHours",
        name="Power on hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    NakedSensorDescription(
        key="software_ver",
        name="Firmware version",
        source="text",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
        entity_registry_enabled_default=False,
    ),
)


def _num(props: dict[str, Any], key: str) -> int | None:
    item = props.get(key) or {}
    raw = item.get("numberValue", item.get("value"))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _timer(n: int) -> Callable[[dict[str, Any]], Any]:
    def fn(props: dict[str, Any]) -> str | None:
        parts = [_num(props, f"T{n}_{k}") for k in ("OnHH", "OnMM", "OffHH", "OffMM")]
        if any(p is None for p in parts):
            return None
        on_h, on_m, off_h, off_m = parts
        if (on_h, on_m) == (off_h, off_m):
            return "Off"
        return f"{on_h:02d}:{on_m:02d}-{off_h:02d}:{off_m:02d}"
    return fn


# Created only on devices that report them (chlorinators).
CHLORINATOR_SENSORS: tuple[NakedSensorDescription, ...] = (
    NakedSensorDescription(
        # When the running boost ends; HA shows it as a countdown.
        key="boost_ends",
        requires="OutputSetPoint",
        name="Chlorine boost ends",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:rocket-launch-outline",
        value_fn=lambda props: _epoch((props.get("BoostEndsAt") or {}).get("value")),
    ),
    NakedSensorDescription(
        # The figure the unit (and the Naked Connect app) shows at the end of a salt test.
        key="SaltTestResult",
        requires="OutputSetPoint",
        name="Salt reading",
        native_unit_of_measurement="ppm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:shaker-outline",
    ),
    NakedSensorDescription(
        # The unit's verdict on the last salt test, e.g. "TDS perfect".
        key="salt_condition",
        requires="OutputSetPoint",
        name="Salt condition",
        source="text",
        icon="mdi:shaker-outline",
        value_fn=lambda props: condition_text((props.get("SaltTestCondition") or {}).get("value")),
    ),
    NakedSensorDescription(
        key="fault_status",
        prop="Fault",
        requires="Fault",
        name="Status",
        source="text",
        icon="mdi:alert-circle-outline",
        value_fn=lambda props: fault_text((props.get("Fault") or {}).get("value")),
    ),
    NakedSensorDescription(
        # Reported in whole degrees (26 = 26°C), unlike the water probes.
        key="power_pack_temp",
        prop="internal_temperature",
        requires="internal_temperature",
        name="Power pack temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NakedSensorDescription(
        key="SpecMode",
        name="Special mode",
        source="text",
        icon="mdi:star-cog-outline",
        requires="OutputSetPoint",
        value_map={"0": "Normal", "1": "Backwash", "2": "Boost", "3": "Salt test",
                   "4": "Output test", "5": "Pump single"},
    ),
    NakedSensorDescription(
        key="ControlMode",
        name="Running on",
        source="text",
        icon="mdi:timer-cog-outline",
        requires="OutputSetPoint",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_map={"NULL": "Idle", "T1": "Timer 1", "T2": "Timer 2", "P1": "Pump timer 1",
                   "P2": "Pump timer 2", "EX": "External control", "HP": "Heat pump"},
    ),
    NakedSensorDescription(
        key="SaltTestLevel",
        name="Salt test result",
        native_unit_of_measurement="ppm",
        icon="mdi:shaker-outline",
        requires="SaltTestLevel",
    ),
    NakedSensorDescription(
        key="pHRunLeft",
        name="pH dose remaining",
        icon="mdi:timer-sand",
        requires="pHRunLeft",
    ),
    NakedSensorDescription(
        key="timer1",
        name="Timer 1",
        icon="mdi:clock-outline",
        requires="T1_OnHH",
        value_fn=_timer(1),
    ),
    NakedSensorDescription(
        key="timer2",
        name="Timer 2",
        icon="mdi:clock-outline",
        requires="T2_OnHH",
        value_fn=_timer(2),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NakedCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [NakedSensor(coordinator, desc) for desc in SENSORS]
    entities += [
        NakedSensor(coordinator, desc)
        for desc in CHLORINATOR_SENSORS
        if coordinator.has(desc.requires or desc.key)
    ]
    entities += copper_sensors(coordinator)
    async_add_entities(entities)


class NakedSensor(NakedEntity, SensorEntity):
    """A single reading from a Naked Connect device."""

    entity_description: NakedSensorDescription

    def __init__(
        self, coordinator: NakedCoordinator, description: NakedSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        # The hub labels its two temperature probes (e.g. "Spa Temp." / "Pool Temp.").
        label_key = TEMP_LABELS.get(description.key)
        if label_key:
            label = self._prop_value(label_key)
            if isinstance(label, str) and label.strip().lower() not in ("", "none", "null"):
                base = label.strip().rstrip(".").replace("Temp", "temperature")
                self._attr_name = base[0].upper() + base[1:].lower()

    @property
    def native_value(self):
        desc = self.entity_description
        if desc.value_fn is not None:
            return desc.value_fn((self.coordinator.data or {}).get("properties") or {})

        item = self._prop(desc.prop or desc.key)
        if item is None:
            return None

        if desc.source == "text":
            value = item.get("value")
            if desc.value_map and value is not None:
                return desc.value_map.get(str(value), value)
            return value

        raw = item.get("numberValue")
        if raw is None:
            raw = item.get("value")
        if raw is None:
            return None
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            return item.get("value")

        divisor = self.entity_description.divide
        if divisor and divisor != 1.0:
            return round(raw / divisor, 1)
        # Keep whole numbers clean.
        return int(raw) if raw == int(raw) else raw
