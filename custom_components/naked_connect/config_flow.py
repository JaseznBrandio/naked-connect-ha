"""Config flow for Naked Connect."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NakedAuthError, NakedClient, NakedConnectionError
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SHOW_PANEL,
    CONF_USERNAME,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


class NakedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID].strip()
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()

            client = NakedClient(
                session=async_get_clientsession(self.hass),
                host=user_input[CONF_HOST].strip(),
                username=user_input[CONF_USERNAME].strip(),
                password=user_input[CONF_PASSWORD],
                device_id=device_id,
            )
            try:
                await client.async_get_properties()
            except NakedAuthError:
                errors["base"] = "invalid_auth"
            except NakedConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Naked Connect ({device_id[-4:]})",
                    data={
                        CONF_HOST: user_input[CONF_HOST].strip(),
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_ID: device_id,
                    },
                    options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL, CONF_SHOW_PANEL: True},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return NakedOptionsFlow(entry)


class NakedOptionsFlow(OptionsFlow):
    """Let the user change the polling interval."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        show_panel = self._entry.options.get(CONF_SHOW_PANEL, True)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=30, max=3600)
                ),
                vol.Required(CONF_SHOW_PANEL, default=show_panel): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
