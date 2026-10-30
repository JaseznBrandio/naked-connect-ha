"""Constants for the Naked Connect integration."""
from __future__ import annotations

DOMAIN = "naked_connect"
VERSION = "0.1.0"

# JetLinks backend the Naked Connect app talks to.
DEFAULT_HOST = "http://47.236.42.212:8850"

# Login credentials are not shipped with the integration. The installer
# enters the username and password supplied by Naked Connect.

DEFAULT_SCAN_INTERVAL = 30  # seconds
OLD_DEFAULT_SCAN_INTERVAL = 300  # default before 0.4.2

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SHOW_PANEL = "show_panel"

PLATFORMS = ["sensor", "binary_sensor", "switch", "select", "number", "button", "light"]

# Chlorinator faults. The unit reports one number made of flags added together
# (one flag per fault), so several faults at once arrive as their sum,
# e.g. Low salt (0x0004) + Water temp low (0x0040) = 0x0044 = 68.
# From the Naked Connect fault table; "TDS" is the unit's term for salt.
FAULT_FLAGS: tuple[tuple[int, str], ...] = (
    (0x0010, "Low flow"),  # water flow fault
    (0x0400, "Extremely high salt"),
    (0x0001, "High salt"),
    (0x0004, "Low salt"),
    (0x0008, "No output"),
    (0x0200, "Low output"),
    (0x0002, "Internal temp high"),
    (0x0100, "Transformer temp high"),
    (0x0020, "Water temp high"),
    (0x0080, "Water temp rising fast"),
    (0x0040, "Water temp low"),
    (0x0800, "Check ioniser rods"),
)


def fault_list(code) -> list[str] | None:
    """Every fault in the reported number, in plain words ([] when there are none)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    faults = [name for flag, name in FAULT_FLAGS if code & flag]
    unknown = code & ~sum(flag for flag, _ in FAULT_FLAGS)
    if unknown:
        faults.append(f"Code 0x{unknown:04X}")
    return faults


def fault_text(code) -> str | None:
    """"OK", or the active faults joined, e.g. "Low salt, Water temp low"."""
    faults = fault_list(code)
    if faults is None:
        return None
    return ", ".join(faults) if faults else "OK"


# Pool light types ("light type" in the Naked Connect app, reported as the socket's
# "manufacturer"). Each maps the colour/show code the hub takes to a name.
# A colour only takes effect while the light is on.
LIGHT_PROFILES: dict[str, dict[str, str]] = {
    "AQUAQUIP_INS": {  # Aqua-Quip InstaTouch
        "L2_AQUA": "Aqua",
        "L2_BLUE": "Blue",
        "L2_GREEN": "Green",
        "L2_GOLD": "Gold",
        "L2_MAGENTA": "Magenta",
        "L2_RED": "Red",
        "L2_WHITE": "White",
        "L2_SEASIDE": "Seaside",
        "L2_SLOW_SCROLL": "Slow scroll",
        "L2_FAST_SCROLL": "Fast scroll",
        "L2_FIREWORKS": "Fireworks",
        "L2_DISCO": "Disco",
        "L2_FLASH": "Flash",
    },
}
# Light types in the app, by the name the hub reports (for showing which one is set).
LIGHT_TYPE_NAMES: dict[str, str] = {
    "AQUAQUIP_INS": "Aqua-Quip InstaTouch",
    "SINGLE_COLOUR": "Single colour",
}
