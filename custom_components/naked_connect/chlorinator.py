"""Chlorinator controls: power mode, output, winter/spa modes, pH dosing, boost, salt test.

Each control is created only when the device reports the property it drives,
so a controller without a salt cell doesn't get chlorinator entities.

Settings are written exactly as the Naked Connect app writes them. Boost and the salt
test are device functions (optionRequest with menu M4 / M10), also as the app
sends them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.number import (
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.exceptions import HomeAssistantError

from .coordinator import NakedCoordinator
from .entity import NakedEntity

# The setting that tells us a device has a salt cell.
CHLORINATOR_MARKER = "OutputSetPoint"


# --------------------------------------------------------------------- selects


@dataclass(frozen=True, kw_only=True)
class ChlorSelectDescription(SelectEntityDescription):
    prop: str
    choices: dict[str, str]  # label -> value sent to the device


CHLOR_SELECTS: tuple[ChlorSelectDescription, ...] = (
    ChlorSelectDescription(
        key="PowerMode_ctl",
        prop="PowerMode",
        name="Chlorinator mode",
        icon="mdi:power",
        choices={"On": "ON", "Off": "OFF", "Auto": "AUTO"},
    ),
    ChlorSelectDescription(
        key="ReriodSet_ctl",
        prop="ReriodSet",
        name="Timer cycle",
        icon="mdi:timer-outline",
        entity_category=EntityCategory.CONFIG,
        choices={"Single": "SINGLE_CYCLE", "Double": "DOUBLE_CYCLE"},
    ),
)


WORK_MODES = {"Normal": "NULL", "Winter": "WINTER", "Spa": "SPA"}
# Switch property and its output setting for each special work mode.
MODE_SWITCH = {"WINTER": ("WinterMode", "WinterModeSet"), "SPA": ("SpaMode", "SpaModeSet")}


def active_mode(coordinator: NakedCoordinator) -> str:
    """NULL (normal), WINTER or SPA, from what the unit reports."""
    props = (coordinator.data or {}).get("properties") or {}
    mode = str((props.get("WorkMode") or {}).get("value", "NULL")).upper()
    return mode if mode in MODE_SWITCH else "NULL"


class WorkModeSelect(NakedEntity, SelectEntity):
    """Normal / Winter / Spa. Shows the unit's reported work mode, so app changes show too."""

    _attr_icon = "mdi:pool"
    _attr_options = list(WORK_MODES)
    _attr_name = "Work mode"

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "WorkMode_ctl")

    @property
    def current_option(self) -> str | None:
        props = (self.coordinator.data or {}).get("properties") or {}
        raw = str((props.get("WorkMode") or {}).get("value", "")).upper()
        for label, value in WORK_MODES.items():
            if raw == value:
                return label
        return None  # boost, salt test etc. are shown by Special mode

    async def async_select_option(self, option: str) -> None:
        target = WORK_MODES[option]
        current = active_mode(self.coordinator)
        if target == current:
            return
        props = (self.coordinator.data or {}).get("properties") or {}
        # The output shown belongs to the old mode; let the unit's report decide it now.
        self.coordinator.forget_pending("OutputSetPoint", "WorkMode")
        # Leave the current special mode first (the unit has one switch per mode).
        if current in MODE_SWITCH:
            switch, _ = MODE_SWITCH[current]
            show = {"WorkMode": "NULL"} if target == "NULL" else None
            await self.coordinator.async_write({switch: "OFF"}, show=show)
        elif target == "NULL":
            # Unknown starting point: make sure both are off.
            await self.coordinator.async_write({"WinterMode": "OFF"}, show={"WorkMode": "NULL"})
            await self.coordinator.async_write({"SpaMode": "OFF"})
        if target in MODE_SWITCH:
            switch, set_key = MODE_SWITCH[target]
            show = {"WorkMode": target}
            level = (props.get(set_key) or {}).get("value")
            if level is not None:
                show["OutputSetPoint"] = level
            await self.coordinator.async_write({switch: "ON"}, show=show)


class ChlorSelect(NakedEntity, SelectEntity):
    entity_description: ChlorSelectDescription

    def __init__(self, coordinator: NakedCoordinator, desc: ChlorSelectDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._attr_options = list(desc.choices)

    @property
    def current_option(self) -> str | None:
        value = self._prop_value(self.entity_description.prop)
        for label, raw in self.entity_description.choices.items():
            if str(value).upper() == raw:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write(
            {self.entity_description.prop: self.entity_description.choices[option]}
        )


# --------------------------------------------------------------------- numbers


@dataclass(frozen=True, kw_only=True)
class ChlorNumberDescription(NumberEntityDescription):
    prop: str


CHLOR_NUMBERS: tuple[ChlorNumberDescription, ...] = (
    ChlorNumberDescription(
        key="WinterModeSet_ctl",
        prop="WinterModeSet",
        name="Winter mode output",
        icon="mdi:snowflake",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    ChlorNumberDescription(
        key="SpaModeSet_ctl",
        prop="SpaModeSet",
        name="Spa mode output",
        icon="mdi:hot-tub",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.CONFIG,
    ),
    ChlorNumberDescription(
        key="pHRunForPerDay_ctl",
        prop="pHRunForPerDay",
        name="pH1 run time",
        icon="mdi:timer-outline",
        native_min_value=0,
        native_max_value=1440,
        native_step=1,
        native_unit_of_measurement="min/day",
        mode=NumberMode.BOX,
    ),
)


class ActiveOutputNumber(NakedEntity, NumberEntity):
    """Output for the current work mode: normal setpoint, winter output or spa output."""

    _attr_name = "Output setpoint"
    _attr_icon = "mdi:water-percent"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "OutputSetPoint_ctl")

    def _setting(self) -> str:
        mode = active_mode(self.coordinator)
        return MODE_SWITCH[mode][1] if mode in MODE_SWITCH else "OutputSetPoint"

    @property
    def native_value(self) -> float | None:
        # The unit reports the active mode's output as its setpoint (e.g. 70 in winter).
        item = self._prop("OutputSetPoint") or self._prop(self._setting()) or {}
        raw = item.get("numberValue", item.get("value"))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"applies_to": {"OutputSetPoint": "Normal", "WinterModeSet": "Winter",
                               "SpaModeSet": "Spa"}[self._setting()]}

    async def async_set_native_value(self, value: float) -> None:
        level = int(round(value))
        await self.coordinator.async_write({self._setting(): level},
                                           show={"OutputSetPoint": level})


class ChlorNumber(NakedEntity, NumberEntity):
    entity_description: ChlorNumberDescription

    def __init__(self, coordinator: NakedCoordinator, desc: ChlorNumberDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> float | None:
        item = self._prop(self.entity_description.prop) or {}
        raw = item.get("numberValue", item.get("value"))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write({self.entity_description.prop: int(round(value))})


class BoostHours(NakedEntity, RestoreNumber):
    """How long boost runs when started from Home Assistant. Stored locally."""

    _attr_name = "Chlorine boost duration"
    _attr_icon = "mdi:rocket-launch-outline"
    _attr_native_min_value = 1
    _attr_native_max_value = 24
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "boost_hours")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value:
            self.coordinator.boost_hours = int(last.native_value)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.boost_hours)

    async def async_set_native_value(self, value: float) -> None:
        if self.coordinator.boost_ends is not None:
            raise HomeAssistantError("Stop the chlorine boost before changing its duration.")
        self.coordinator.boost_hours = int(value)
        self.async_write_ha_state()


class PhAmount(NakedEntity, RestoreNumber):
    """pH2 prime / demand amount in ml. Only sent when Start Prime / Demand Dosing is pressed."""

    _attr_name = "pH2 Prime / Demand amount"
    _attr_icon = "mdi:beaker-outline"
    _attr_native_min_value = 10
    _attr_native_max_value = 2000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = "mL"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: NakedCoordinator) -> None:
        super().__init__(coordinator, "ph_amount")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last and last.native_value:
            self.coordinator.ph_amount = int(last.native_value)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.ph_amount)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.ph_amount = int(value)
        self.async_write_ha_state()


# -------------------------------------------------------------------- switches


@dataclass(frozen=True, kw_only=True)
class ChlorSwitchDescription(SwitchEntityDescription):
    prop: str
    on_value: str = "ON"
    off_value: str = "OFF"


CHLOR_SWITCHES: tuple[ChlorSwitchDescription, ...] = (
    ChlorSwitchDescription(key="pHSwitch_ctl", prop="pHSwitch", name="pH dosing",
                           icon="mdi:beaker-outline"),
)


class ChlorSwitch(NakedEntity, SwitchEntity):
    entity_description: ChlorSwitchDescription

    def __init__(self, coordinator: NakedCoordinator, desc: ChlorSwitchDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        value = self._prop_value(self.entity_description.prop)
        if value is None:
            return None
        return str(value).upper() in (self.entity_description.on_value, "1")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            {self.entity_description.prop: self.entity_description.on_value}
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(
            {self.entity_description.prop: self.entity_description.off_value}
        )


# --------------------------------------------------------------------- buttons


class ChlorButton(NakedEntity, ButtonEntity):
    """Runs a device function the same way the Naked Connect app does."""

    def __init__(self, coordinator: NakedCoordinator, desc: ButtonEntityDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    async def async_press(self) -> None:
        key = self.entity_description.key
        hours = self.coordinator.boost_hours
        if key == "boost_start":
            # The app opens the boost screen (menu M4) before pressing start.
            await self.coordinator.async_invoke(
                "optionRequest", {"menu": "M4", "button": "NULL", "param": "NULL"}
            )
            await self.coordinator.async_invoke(
                "optionRequest",
                {"menu": "M4", "button": "CHLORBOOST=START", "param": f"RUNHOURS={hours}"},
                wait=False,
            )
            self.coordinator.set_boost(time.time() + hours * 3600)
            self.coordinator.hold("SpecMode", 2)
        elif key == "boost_stop":
            await self.coordinator.async_invoke(
                "optionRequest",
                {"menu": "M4", "button": "CHLORBOOST=STOP", "param": f"RUNHOURS={hours}"},
                wait=False,
            )
            self.coordinator.set_boost(None)
            self.coordinator.hold("SpecMode", 0)
        elif key == "ph_start":
            # Same as the app: sending the amount starts a dose run.
            await self.coordinator.async_write({"pHRunAtDose": self.coordinator.ph_amount})
        elif key == "ph_stop":
            await self.coordinator.async_write({"pHRunLeft": 0})
        elif key == "salt_test":
            # Opening the salt test screen (menu M10) runs the test.
            await self.coordinator.async_invoke(
                "optionRequest", {"menu": "M10", "button": "NULL", "param": "NULL"}
            )


CHLOR_BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(key="boost_start", name="Start chlorine boost", icon="mdi:rocket-launch"),
    ButtonEntityDescription(key="boost_stop", name="Stop chlorine boost", icon="mdi:stop-circle-outline"),
    ButtonEntityDescription(key="salt_test", name="Test Salt Level", icon="mdi:shaker-outline"),
    ButtonEntityDescription(key="ph_start", name="Start Prime / Demand Dosing", icon="mdi:play-circle-outline"),
    ButtonEntityDescription(key="ph_stop", name="Stop Prime / Demand Dosing", icon="mdi:stop-circle-outline"),
)


# ---------------------------------------------------------------- factories


def is_chlorinator(coordinator: NakedCoordinator) -> bool:
    return coordinator.has(CHLORINATOR_MARKER)


def chlor_selects(c: NakedCoordinator) -> list[SelectEntity]:
    ents: list[SelectEntity] = [ChlorSelect(c, d) for d in CHLOR_SELECTS if c.has(d.prop)]
    if is_chlorinator(c) and c.has("WorkMode"):
        ents.insert(1, WorkModeSelect(c))
    return ents


def chlor_numbers(c: NakedCoordinator) -> list[NumberEntity]:
    ents: list[NumberEntity] = [ChlorNumber(c, d) for d in CHLOR_NUMBERS if c.has(d.prop)]
    if is_chlorinator(c):
        ents.insert(0, ActiveOutputNumber(c))
        ents.append(BoostHours(c))
        if c.has("pHSwitch"):
            ents.append(PhAmount(c))
    return ents


def chlor_switches(c: NakedCoordinator) -> list[ChlorSwitch]:
    return [ChlorSwitch(c, d) for d in CHLOR_SWITCHES if c.has(d.prop)]


def chlor_buttons(c: NakedCoordinator) -> list[ChlorButton]:
    return [ChlorButton(c, d) for d in CHLOR_BUTTONS] if is_chlorinator(c) else []
