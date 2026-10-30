"""Shared base entities for Naked Connect."""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import NakedCoordinator


def device_info(coordinator: NakedCoordinator) -> DeviceInfo:
    """Name the device after what it reports; anything unrecognised gets a generic name.

    Users can rename it in Home Assistant (e.g. "Water filler") and the panel follows.
    """
    props = (coordinator.data or {}).get("properties") or {}
    sw = (props.get("software_ver") or {}).get("value")
    if "OutputSetPoint" in props:  # has a salt cell
        name, model = "Naked Connect", "Freshwater mineral system"
    elif coordinator.sockets:  # reports switched sockets
        name, model = "Naked Connect Controller", "Controller hub"
    else:
        name, model = "Naked Connect Device", "Naked Connect device"
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.client.device_id)},
        name=name,
        manufacturer="Naked",
        model=model,
        sw_version=sw,
    )


class NakedEntity(CoordinatorEntity[NakedCoordinator]):
    """Base for entities backed by a device property."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NakedCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.client.device_id}_{key}"
        self._attr_device_info = device_info(coordinator)

    def _prop(self, key: str) -> dict[str, Any] | None:
        props = (self.coordinator.data or {}).get("properties") or {}
        return props.get(key)

    def _prop_value(self, key: str) -> Any | None:
        item = self._prop(key)
        return None if item is None else item.get("value")


# ---------------------------------------------------------------- sockets

_CYCLE_COUNT = {"SINGLE_CYCLE": 1, "DOUBLE_CYCLE": 2}


def pretty_socket_name(raw: str | None, socket: str) -> str:
    if not raw or raw.lower() in ("none", "null"):
        return f"Socket {socket[6:]}"
    return raw.replace("_", " ").strip().title()


def socket_display_name(coordinator: NakedCoordinator, socket: str) -> str:
    """Reported name, with the socket number added when two share a name."""
    sockets = coordinator.sockets
    name = pretty_socket_name(sockets.get(socket, {}).get("fields", {}).get("name"), socket)
    same = [
        s for s, v in sockets.items()
        if pretty_socket_name(v.get("fields", {}).get("name"), s) == name
    ]
    return f"{name} (socket {socket[6:]})" if len(same) > 1 else name


def _parse_hhmm(value: str | None) -> time | None:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def active_timers(fields: dict[str, str]) -> list[tuple[time, time]]:
    """The timer windows the socket follows in AUTO mode."""
    count = _CYCLE_COUNT.get(str(fields.get("TimerMode", "")).upper(), 4)
    windows = []
    for i in range(1, count + 1):
        on, off = _parse_hhmm(fields.get(f"T{i}_On")), _parse_hhmm(fields.get(f"T{i}_Off"))
        if on is not None and off is not None and on != off:
            windows.append((on, off))
    return windows


def timer_says_on(fields: dict[str, str], now: datetime | None = None) -> bool:
    current = (now or dt_util.now()).time()
    for on, off in active_timers(fields):
        if on < off and on <= current < off:
            return True
        if on > off and (current >= on or current < off):  # window crosses midnight
            return True
    return False


def socket_is_on(fields: dict[str, str]) -> bool | None:
    """Best estimate of whether the socket is powered right now."""
    sw = str(fields.get("SW", "")).upper()
    if sw == "ON":
        return True
    if sw == "OFF":
        return False
    if sw == "AUTO":
        if str(fields.get("oncesw", "")).upper() == "ON":
            return True
        return timer_says_on(fields)
    return None


class NakedSocketEntity(CoordinatorEntity[NakedCoordinator]):
    """Base for entities backed by one SOCKETn."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NakedCoordinator, socket: str, suffix: str) -> None:
        super().__init__(coordinator)
        self._socket = socket
        self._attr_unique_id = f"{coordinator.client.device_id}_{socket.lower()}_{suffix}"
        self._attr_device_info = device_info(coordinator)

    @property
    def _fields(self) -> dict[str, str]:
        return (self.coordinator.sockets.get(self._socket) or {}).get("fields") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._fields.get("SW"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        f = self._fields
        return {
            "socket": self._socket,
            "mode": f.get("SW"),
            "timer_mode": f.get("TimerMode"),
            "timers": [f"{a:%H:%M}-{b:%H:%M}" for a, b in active_timers(f)],
            "reported_name": f.get("name"),
        }
