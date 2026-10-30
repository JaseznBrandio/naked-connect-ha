"""Sidebar panel and the websocket API it uses."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_SHOW_PANEL, DOMAIN, VERSION

PANEL_URL_PATH = "naked-connect"
PANEL_COMPONENT = "naked-connect-panel"
STATIC_URL = "/naked_connect_static"
_DATA = f"{DOMAIN}_panel"

# unique_id suffix -> role used by the panel
READING_KEYS = {
    "WaterTemp": "temp_a",
    "WaterTempB": "temp_b",
    "SaltLevel": "salt",
    "ActualOutput": "output",
    "OutputSetPoint": "setpoint",
    "ChlorineProduction": "chlorine",
    "CellStatus": "cell",
    "WorkMode": "work_mode",
    "PowerMode": "power_mode",
    "Fault": "fault",
    "PumpStatus": "pump",
    # chlorinator
    "SpecMode": "special",
    "power_pack_temp": "power_pack",
    "boost_ends": "boost_ends",
    "boost_hours": "boost_hours",
    "fault_status": "fault_text",
    "SaltTestResult": "salt_test_result",
    "PowerMode_ctl": "mode",
    "OutputSetPoint_ctl": "setpoint",
    "WorkMode_ctl": "work_mode_ctl",
    "pHStatus": "ph_status",
    "pHRunLeft": "ph_left",
    "pHRunForPerDay_ctl": "ph_runtime",
    "ph_amount": "ph_amount",
    "ph_start": "ph_start",
    "ph_stop": "ph_stop",
    "pHSwitch_ctl": "ph",
    "boost_start": "boost_start",
    "boost_stop": "boost_stop",
    "salt_test": "salt_test",
    "salt_condition": "salt_condition",
    # copper ioniser
    "copper_output": "copper_output",
    "copper_level": "copper_level",
    "copper_running": "copper_running",
    "copper_boost_active": "copper_boost",
    "copper_boost_ends": "copper_boost_ends",
    "copper_boost_start": "copper_boost_start",
    "copper_boost_stop": "copper_boost_stop",
}


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register static files, websocket command and the sidebar panel (once)."""
    state = hass.data.setdefault(_DATA, {"static": False, "panel": False})

    if not state["static"]:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_URL, str(Path(__file__).parent / "frontend"), cache_headers=False
                )
            ]
        )
        websocket_api.async_register_command(hass, ws_list_devices)
        state["static"] = True

    if not state["panel"]:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_COMPONENT,
            module_url=f"{STATIC_URL}/naked-connect-panel.js?v={VERSION}",
            sidebar_title="Naked Connect",
            sidebar_icon="mdi:pool",
            require_admin=False,
            config={},
        )
        state["panel"] = True


@callback
def async_maybe_remove_panel(hass: HomeAssistant, unloading_entry_id: str) -> None:
    """Remove the panel when no remaining loaded entry wants it."""
    state = hass.data.get(_DATA)
    if not state or not state["panel"]:
        return
    still_wanted = any(
        e.entry_id != unloading_entry_id
        and e.state is ConfigEntryState.LOADED
        and e.options.get(CONF_SHOW_PANEL, True)
        for e in hass.config_entries.async_entries(DOMAIN)
    )
    if not still_wanted:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
        state["panel"] = False


@websocket_api.websocket_command({vol.Required("type"): "naked_connect/devices"})
@callback
def ws_list_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List configured Naked Connect devices and the entity ids the panel should show."""
    registry = er.async_get(hass)
    devices = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is None:
            continue
        device_id_ = coordinator.client.device_id
        prefix = f"{device_id_}_"
        readings: dict[str, str] = {}
        sockets: dict[str, dict[str, Any]] = {}
        ha_device_id = None

        for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
            if ent.disabled_by is not None or not ent.unique_id.startswith(prefix):
                continue
            ha_device_id = ha_device_id or ent.device_id
            key = ent.unique_id[len(prefix):]
            if key in READING_KEYS:
                readings[READING_KEYS[key]] = ent.entity_id
                continue
            if key.startswith("socket") and "_" in key:
                sock, kind = key.split("_", 1)
                if kind in ("switch", "mode", "light"):
                    item = sockets.setdefault(sock.upper(), {"socket": sock.upper()})
                    item[kind] = ent.entity_id

        props = (coordinator.data or {}).get("properties") or {}
        devices.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "device_id": ha_device_id,
                "sw_version": (props.get("software_ver") or {}).get("value"),
                "last_update_success": coordinator.last_update_success,
                "readings": readings,
                "sockets": sorted(
                    sockets.values(), key=lambda s: int(s["socket"][6:] or 0)
                ),
            }
        )
    connection.send_result(msg["id"], {"version": VERSION, "devices": devices})
