"""DataUpdateCoordinator for Naked Connect."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from urllib.parse import parse_qsl

from .salt_test import parse_reading, summarise
from .api import SOCKET_KEYS, NakedAuthError, NakedClient, NakedConnectionError, NakedError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
CONFIRM_DELAY = 4  # seconds before the first check after a command
# After a command, poll quickly while the device works through it: a
# chlorinator purges for ~16s, then ramps its output for another ~15s.
FAST_INTERVAL = timedelta(seconds=5)
FAST_WINDOW = 90  # seconds of fast polling after each command
# While the device is mid-transition on its own (purging, salt test), keep
# checking every 10s, but never for longer than this.
SETTLING_INTERVAL = timedelta(seconds=10)
SETTLING_MAX = 600
# While live updates are flowing, polling is only a safety net.
PUSH_FALLBACK_INTERVAL = timedelta(seconds=300)
TRIGGER_WAIT = 3  # seconds for the hub to answer a full-report request
DEFAULT_BOOST_HOURS = 8
DEFAULT_PH_AMOUNT = 100  # ml, pH demand/prime amount sent with Start
SALT_TEST_SETTLE = 8  # seconds without readings that mark the end of a salt test
PENDING_HOLD = 20  # seconds to keep showing a change the unit hasn't confirmed yet
# Settings the chlorinator doesn't (reliably) report back after a change; their
# current value is taken from the cloud's record of writes when that is newer.
WRITTEN_SETTINGS = (
    "WinterMode", "SpaMode", "pHSwitch", "WinterModeSet", "SpaModeSet",
    "pHRunForPerDay", "pHRunAtDose", "ReriodSet", "ION_SET",
)


class NakedCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls readings and socket states.

    data = {"properties": {prop_id: item}, "sockets": {"SOCKET2": {"ts", "fields"}}}
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: NakedClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {client.device_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.entry = entry
        self._normal_interval = timedelta(seconds=scan_interval)
        self._fast_until = 0.0
        self._settling_since: float | None = None
        # Changes made from HA, shown straight away and held until the unit reports
        # the same value (confirmed) or PENDING_HOLD passes; after that whatever the
        # unit last reported is shown. Comparing values, not clocks, means a stale
        # report can't bounce a change back, and a change the unit didn't carry out
        # can't stay on screen.
        #   property -> (value, give_up_at_monotonic)
        self._pending: dict[str, tuple[Any, float]] = {}
        #   SOCKETn -> ({field: value}, give_up_at_monotonic), e.g. {"SW": "ON", "color": ..}
        self._socket_hold: dict[str, tuple[dict[str, str], float]] = {}
        # Last properties as the unit reported them (before any held change).
        self._raw: dict[str, dict[str, Any]] = {}
        self.ph_amount: int = DEFAULT_PH_AMOUNT
        self._salt_test: dict[str, Any] | None = None
        self._salt_session: list[tuple[int, str]] = []
        self._salt_timer = None
        self.push_connected = False
        # When the running boost ends (epoch seconds), learned from the unit's
        # boost screen ("Hour=8&Minute=0&Second=0") and kept across restarts.
        self.boost_ends: float | None = None
        self._boost_asked = 0.0
        self._state_store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.state"
        )
        self._salt_checked = 0.0
        self._was_testing = False
        # Socket state is only visible when the hub reports a change, so the
        # last known state is cached here and persisted across restarts.
        self._sockets: dict[str, dict[str, Any]] = {}
        # Boost duration (hours) sent with "start boost"; set by a number entity.
        self.boost_hours: int = DEFAULT_BOOST_HOURS
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.sockets"
        )

    @property
    def sockets(self) -> dict[str, dict[str, Any]]:
        """Socket state as shown: what the hub reported, plus changes not yet confirmed."""
        if self.data is not None and "sockets" in self.data:
            return self.data["sockets"]
        return self._sockets

    async def async_load_cache(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            for sock, fields in stored.items():
                if isinstance(fields, dict):
                    self._sockets[sock] = {"ts": 0, "fields": fields}

    async def async_load_state(self) -> None:
        stored = await self._state_store.async_load()
        if isinstance(stored, dict) and isinstance(stored.get("boost_ends"), (int, float)):
            self.boost_ends = float(stored["boost_ends"])

    @callback
    def _save_state(self) -> None:
        self._state_store.async_delay_save(lambda: {"boost_ends": self.boost_ends}, 2)

    @callback
    def set_boost(self, ends: float | None) -> None:
        if ends != self.boost_ends:
            self.boost_ends = ends
            self._save_state()

    @callback
    def boost_from_param(self, param: Any) -> bool:
        """Set the boost end time from the boost screen's "Hour=..&Minute=..&Second=.."."""
        if not isinstance(param, str) or "Hour=" not in param:
            return False
        fields = dict(parse_qsl(param))
        try:
            secs = int(fields.get("Hour", 0)) * 3600 + int(fields.get("Minute", 0)) * 60 + int(
                fields.get("Second", 0))
        except ValueError:
            return False
        self.set_boost(time.time() + secs if secs > 0 else None)
        return True

    def _inject_boost(self, props: dict[str, Any]) -> dict[str, Any]:
        spec = str((props.get("SpecMode") or {}).get("value", ""))
        if spec and spec != "2" and self.boost_ends is not None:
            self.set_boost(None)  # boost finished or was stopped
        if self.boost_ends is not None and self.boost_ends < time.time():
            self.set_boost(None)
        props = dict(props)
        if self.boost_ends is not None:
            props["BoostEndsAt"] = {"property": "BoostEndsAt", "value": self.boost_ends}
        else:
            props.pop("BoostEndsAt", None)
        return props

    async def _ask_boost_remaining(self, props: dict[str, Any]) -> None:
        """Boost running but end time unknown (e.g. after a restart): ask the boost screen."""
        if str((props.get("SpecMode") or {}).get("value", "")) != "2" or self.boost_ends:
            return
        if time.monotonic() - self._boost_asked < 600:
            return
        self._boost_asked = time.monotonic()
        try:
            reply = await self.client.async_invoke(
                "optionRequest", {"menu": "M4", "button": "NULL", "param": "NULL"})
        except NakedError as err:
            _LOGGER.debug("Boost screen read failed: %s", err)
            return
        for item in reply if isinstance(reply, list) else [reply]:
            if isinstance(item, dict) and self.boost_from_param(item.get("param")):
                return

    @callback
    def _save_cache(self) -> None:
        self._store.async_delay_save(
            lambda: {s: v["fields"] for s, v in self._sockets.items()}, 10
        )

    async def _async_update_data(self) -> dict[str, Any]:
        if self.data is None:
            # First start: ask the hub to resend every reading and setting, so
            # values that rarely change (sockets, timers) are available at once.
            try:
                await self.client.async_invoke("triggerReport")
                await asyncio.sleep(TRIGGER_WAIT)
            except NakedError as err:
                _LOGGER.debug("triggerReport failed, continuing: %s", err)
        try:
            props = await self.client.async_get_properties()
        except NakedAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NakedConnectionError as err:
            raise UpdateFailed(str(err)) from err

        chlorinator = "OutputSetPoint" in props
        try:
            if chlorinator and not self._sockets:
                reports = {}  # a chlorinator has no switched sockets
            else:
                # First start with nothing cached: look further back in the log.
                deep = self.data is None and not self._sockets
                reports = await self.client.async_get_socket_reports(deep=deep)
        except NakedError as err:
            _LOGGER.debug("Socket report read failed, keeping cached state: %s", err)
            reports = {}

        changed = False
        for sock, rep in reports.items():
            cur = self._sockets.get(sock)
            if cur is None or rep["ts"] > cur["ts"]:
                self._sockets[sock] = rep
                changed = True
        if changed:
            self._save_cache()

        if chlorinator:
            props = await self._apply_recent_writes(props)
            await self._update_salt_test(props)
            props = self._with_salt_test(props)

        self._raw = props
        if chlorinator:
            await self._ask_boost_remaining(self._apply_pending(props))
        return self._build()

    # ------------------------------------------------------- held changes

    @staticmethod
    def _norm(value: Any) -> Any:
        text = str(value).strip().upper()
        try:
            return float(text)
        except ValueError:
            return text

    @classmethod
    def _same(cls, a: Any, b: Any) -> bool:
        na, nb = cls._norm(a), cls._norm(b)
        if na == nb:
            return True
        # Some switches report 1/0 where the app writes ON/OFF.
        return {na, nb} in ({1.0, "ON"}, {0.0, "OFF"})

    def _apply_pending(self, props: dict[str, Any]) -> dict[str, Any]:
        """Reported properties with unconfirmed changes from HA on top."""
        if not self._pending:
            return props
        now = time.monotonic()
        props = dict(props)
        for key, (value, give_up) in list(self._pending.items()):
            reported = (props.get(key) or {}).get("value")
            if now > give_up or (reported is not None and self._same(reported, value)):
                del self._pending[key]  # confirmed, or show what the unit reports
                continue
            item = {**(props.get(key) or {}), "property": key, "value": value}
            if isinstance(value, (int, float)):
                item["numberValue"] = value
            else:
                item.pop("numberValue", None)
            props[key] = item
        return props

    def _apply_socket_holds(self) -> dict[str, dict[str, Any]]:
        """Reported socket state with unconfirmed changes from HA on top."""
        shown = dict(self._sockets)
        now = time.monotonic()
        for sock, (held, give_up) in list(self._socket_hold.items()):
            fields = (self._sockets.get(sock) or {}).get("fields") or {}
            if now > give_up:
                del self._socket_hold[sock]
                continue
            pending = {k: v for k, v in held.items() if str(fields.get(k, "")).upper() != v.upper()}
            if not pending:
                del self._socket_hold[sock]  # the hub confirmed every held field
                continue
            self._socket_hold[sock] = (pending, give_up)
            cur = self._sockets.get(sock) or {"ts": 0, "fields": {}}
            shown[sock] = {"ts": cur["ts"], "fields": {**cur["fields"], **pending}}
        return shown

    def _build(self) -> dict[str, Any]:
        props = self._apply_pending(self._raw)
        if "OutputSetPoint" in props:
            props = self._inject_boost(props)
        self._pick_interval(props)
        return {"properties": props, "sockets": self._apply_socket_holds()}

    @callback
    def _publish(self) -> None:
        """Show the current state now, without postponing the safety-net poll."""
        if self.data is None:
            return
        self.data = self._build()
        self.async_update_listeners()

    @callback
    def _expire_later(self) -> None:
        """When a hold runs out unconfirmed, show what the unit actually reported."""

        @callback
        def _expired(_now) -> None:
            self._publish()

        async_call_later(self.hass, PENDING_HOLD + 0.5, _expired)

    @callback
    def hold(self, key: str, value: Any) -> None:
        """Show a value straight away, until the unit confirms it or the hold runs out."""
        self._pending[key] = (value, time.monotonic() + PENDING_HOLD)
        self._publish()
        self._expire_later()

    def forget_pending(self, *keys: str) -> None:
        """Stop holding values that a new change makes out of date."""
        for key in keys:
            self._pending.pop(key, None)

    async def _apply_recent_writes(self, props: dict[str, Any]) -> dict[str, Any]:
        """Use the newest written value for settings the device doesn't echo back."""
        try:
            writes = await self.client.async_get_recent_writes()
        except NakedError as err:
            _LOGGER.debug("Write log read failed: %s", err)
            return props
        props = dict(props)
        done: set[str] = set()
        for ts, values in writes:  # newest first
            for key, value in values.items():
                if key not in WRITTEN_SETTINGS or key in done:
                    continue
                done.add(key)
                cur = props.get(key) or {}
                if ts > int(cur.get("timestamp") or 0):
                    item = {**cur, "property": key, "value": value, "timestamp": ts}
                    if isinstance(value, (int, float)):
                        item["numberValue"] = value
                    props[key] = item
        return props

    def _with_salt_test(self, props: dict[str, Any]) -> dict[str, Any]:
        """Add the last salt test result and its verdict as readings."""
        if not self._salt_test:
            return props
        ts = self._salt_test["ts"]
        return {
            **props,
            "SaltTestResult": {"property": "SaltTestResult", "value": self._salt_test["value"],
                               "numberValue": self._salt_test["value"], "timestamp": ts},
            "SaltTestCondition": {"property": "SaltTestCondition",
                                  "value": self._salt_test.get("condition"), "timestamp": ts},
        }

    @callback
    def _salt_finish_later(self) -> None:
        """Record the result once readings stop arriving (the unit sends no clear end)."""
        if self._salt_timer is not None:
            self._salt_timer()

        @callback
        def _done(_now) -> None:
            self._salt_timer = None
            self._finish_salt_test()

        self._salt_timer = async_call_later(self.hass, SALT_TEST_SETTLE, _done)

    @callback
    def _finish_salt_test(self) -> None:
        if self._salt_timer is not None:
            self._salt_timer()
            self._salt_timer = None
        result = summarise(self._salt_session)
        self._salt_session = []
        if result is None:
            return
        self._salt_test = {**result, "ts": int(time.time() * 1000)}
        raw = self._with_salt_test(dict(self._raw))
        # The unit doesn't always report the end of the test; it's over.
        if str((raw.get("SpecMode") or {}).get("value", "")) == "3":
            raw["SpecMode"] = {**raw["SpecMode"], "value": 0, "numberValue": 0}
        self._raw = raw
        self._publish()

    async def _update_salt_test(self, props: dict[str, Any]) -> None:
        """Fetch the last salt test result when one may have just run (or every 5 min)."""
        testing = str((props.get("SpecMode") or {}).get("value", "")) == "3"
        now = time.monotonic()
        # One small log search per check, so a test run from the app shows up too.
        self._was_testing = testing
        try:
            result = await self.client.async_get_salt_test(deep=self._salt_checked == 0.0)
        except NakedError as err:
            _LOGGER.debug("Salt test read failed: %s", err)
            return
        self._salt_checked = now
        if result and (self._salt_test is None or result["ts"] >= self._salt_test["ts"]):
            self._salt_test = result

    @staticmethod
    def _is_settling(props: dict[str, Any]) -> bool:
        """True while the device is in a short-lived state that will change on its own."""
        def val(key: str) -> str:
            return str((props.get(key) or {}).get("value", "")).upper()

        if val("CellStatus") == "PURGE" or val("SpecMode") == "3":  # 3 = salt test
            return True
        # A fault (e.g. low flow) clears itself once the cause is fixed.
        return val("Fault") not in ("", "0", "NONE", "NULL")

    @callback
    def _pick_interval(self, props: dict[str, Any]) -> None:
        now = time.monotonic()
        if self._is_settling(props):
            self._settling_since = self._settling_since or now
        else:
            self._settling_since = None
        if self.push_connected:
            # Changes arrive live; poll only as a safety net.
            self.update_interval = max(self._normal_interval, PUSH_FALLBACK_INTERVAL)
        elif now < self._fast_until:
            self.update_interval = FAST_INTERVAL
        elif self._settling_since is not None and now - self._settling_since < SETTLING_MAX:
            self.update_interval = SETTLING_INTERVAL
        else:
            self.update_interval = self._normal_interval

    async def async_set_socket(self, socket: str, mode: str) -> None:
        """Show the new socket mode at once, send it, and hold it until the hub confirms."""
        socket, mode = socket.upper(), mode.upper()
        self._hold_socket(socket, "SW", mode)
        try:
            await self.client.async_set_socket(socket, mode)
        except (NakedConnectionError, NakedAuthError, ValueError) as err:
            self._release_socket(socket, "SW")
            raise HomeAssistantError(f"Naked Connect: could not set {socket} to {mode}: {err}") from err
        self._expire_later()
        self._schedule_confirm()

    @callback
    def _hold_socket(self, socket: str, field: str, value: str) -> None:
        held, _ = self._socket_hold.get(socket, ({}, 0.0))
        self._socket_hold[socket] = ({**held, field: value}, time.monotonic() + PENDING_HOLD)
        self._publish()

    @callback
    def _release_socket(self, socket: str, field: str) -> None:
        held, give_up = self._socket_hold.get(socket, ({}, 0.0))
        held = {k: v for k, v in held.items() if k != field}
        if held:
            self._socket_hold[socket] = (held, give_up)
        else:
            self._socket_hold.pop(socket, None)
        self._publish()

    def _reported_on(self, socket: str) -> bool:
        """Lit right now, by the hub's report: forced on, or on its timer in Auto."""
        from .entity import socket_is_on  # entity imports this module

        fields = (self._sockets.get(socket) or {}).get("fields") or {}
        return bool(socket_is_on(fields))

    async def async_set_light_color(self, socket: str, code: str) -> None:
        """Set a light's colour or show. The hub only applies it while the light is on,
        so a light that's off (or on its timer) is switched on first."""
        socket = socket.upper()
        if not self._reported_on(socket):
            await self.async_set_socket(socket, "ON")
            # Wait for the hub to confirm it's on (usually about a second).
            for i in range(40):
                if self._reported_on(socket):
                    break
                await asyncio.sleep(0.25)
                if not self.push_connected and i % 6 == 5:
                    await self.async_refresh()  # no live updates: ask every 1.5s
        self._hold_socket(socket, "color", code)
        try:
            await self.client.async_write_properties({socket: f"color={code}"})
        except (NakedConnectionError, NakedAuthError) as err:
            self._release_socket(socket, "color")
            raise HomeAssistantError(f"Naked Connect: could not set the colour: {err}") from err
        self._expire_later()
        self._schedule_confirm()

    @callback
    def _schedule_confirm(self) -> None:
        """Check back soon after a command, then keep checking quickly for a while."""
        self._fast_until = time.monotonic() + FAST_WINDOW
        if self.push_connected:
            return  # the unit's answer arrives on the live channel
        self.update_interval = FAST_INTERVAL

        async def _confirm(_now) -> None:
            await self.async_refresh()

        async_call_later(self.hass, CONFIRM_DELAY, _confirm)

    async def async_write(
        self, values: dict[str, Any], show: dict[str, Any] | None = None,
        *, hold: bool = True,
    ) -> None:
        """Write device settings, show them immediately, confirm shortly after.

        `show` holds extra values to display straight away (e.g. the work mode a
        write will cause), until the device's own report replaces them.
        `hold=False` sends values that are commands rather than settings
        (e.g. the copper boost trigger), so only `show` is displayed.
        """
        held = {**(values if hold else {}), **(show or {})}
        give_up = time.monotonic() + PENDING_HOLD
        for key, value in held.items():
            self._pending[key] = (value, give_up)
        self._publish()  # show it before the cloud answers
        try:
            await self.client.async_write_properties(values)
        except (NakedConnectionError, NakedAuthError) as err:
            for key in held:
                self._pending.pop(key, None)
            self._publish()
            raise HomeAssistantError(f"Naked Connect: could not set {values}: {err}") from err
        self._expire_later()
        self._schedule_confirm()

    async def async_invoke(
        self, function: str, inputs: dict[str, Any] | None = None, *, wait: bool = True
    ) -> None:
        """Call a device function (boost, salt test), then refresh."""
        try:
            await self.client.async_invoke(function, inputs, wait=wait)
        except (NakedConnectionError, NakedAuthError) as err:
            raise HomeAssistantError(f"Naked Connect: {function} failed: {err}") from err
        self._schedule_confirm()

    def has(self, key: str) -> bool:
        """True when the device reports this property (used to pick entities)."""
        return key in ((self.data or {}).get("properties") or {})

    # ------------------------------------------------------------ live updates

    @callback
    def push_state(self, connected: bool) -> None:
        """Live channel opened or dropped."""
        self.push_connected = connected
        props = (self.data or {}).get("properties") or {}
        self._pick_interval(props)
        if not connected:
            # Catch up on anything missed while it was down.
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def handle_push(self, payload: dict[str, Any]) -> None:
        """Apply one message from the live channel straight away."""
        if self.data is None:
            return
        kind = payload.get("messageType")
        now_ms = int(time.time() * 1000)
        ts = payload.get("timestamp")
        # Some replies carry the unit's own clock, which can be years off.
        ts = ts if isinstance(ts, int) and abs(ts - now_ms) < 86_400_000 else now_ms

        updates: dict[str, Any] = {}
        sockets_changed = False
        if kind == "REPORT_PROPERTY":
            for key, value in (payload.get("properties") or {}).items():
                if str(key).upper() in SOCKET_KEYS and isinstance(value, str):
                    # Reported state only; a change from HA is held separately.
                    sock_key = str(key).upper()
                    cur = self._sockets.get(sock_key)
                    if cur is None or ts >= cur["ts"]:
                        self._sockets[sock_key] = {"ts": ts, "fields": dict(parse_qsl(value))}
                        sockets_changed = True
                else:
                    updates[key] = value
        elif kind == "WRITE_PROPERTY":
            # A setting changed by the app (or by HA); some are never reported back.
            for key, value in (payload.get("properties") or {}).items():
                if key in WRITTEN_SETTINGS:
                    updates[key] = value
        elif kind == "EVENT" and payload.get("event") == "menuStatus":
            data = payload.get("data") or {}
            if data.get("menu") == "M4":
                # Boost screen: step 1 carries the time left, step 0 means stopped.
                if data.get("step") == 1:
                    self.boost_from_param(data.get("param"))
                elif data.get("step") == 0:
                    self.set_boost(None)
                updates["SpecMode"] = 2 if self.boost_ends else 0
            elif data.get("menu") == "M10":
                # Salt test screen: collect this test's readings; the result is
                # recorded once they stop (or the screen closes).
                if data.get("step") == 0:
                    self._finish_salt_test()
                else:
                    reading = parse_reading(data.get("param"))
                    if reading is not None:
                        self._salt_session.append(reading)
                        self._salt_finish_later()
        if not updates and not sockets_changed:
            return

        raw = dict(self._raw)
        for key, value in updates.items():
            item = {**(raw.get(key) or {}), "property": key, "value": value, "timestamp": ts}
            if isinstance(value, (int, float)):
                item["numberValue"] = value
            else:
                item.pop("numberValue", None)
            raw[key] = item
        self._raw = raw
        if sockets_changed:
            self._save_cache()
        self._publish()
