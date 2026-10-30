"""API client for the Naked Connect / JetLinks cloud backend."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl

import aiohttp

from .salt_test import SESSION_GAP_MS, parse_reading, summarise

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Long enough for the cloud to pass a command on; the device acts within a second.
_SEND_TIMEOUT = aiohttp.ClientTimeout(total=4)

SOCKET_KEYS = tuple(f"SOCKET{i}" for i in range(1, 10))
SOCKET_MODES = ("ON", "OFF", "AUTO")


class NakedError(Exception):
    """Base error."""


class NakedAuthError(NakedError):
    """Login failed / token rejected."""


class NakedConnectionError(NakedError):
    """Could not reach the backend, or it returned something unexpected."""


class NakedClient:
    """Log in for a token, read the device's state, write settings and call functions."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        device_id: str,
    ) -> None:
        self._session = session
        self._host = host.rstrip("/")
        self._username = username
        self._password = password
        self._device_id = device_id
        self._token: str | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    # ------------------------------------------------------------------ auth

    async def async_login(self) -> str:
        """Authenticate and cache a token. Tokens are short-lived (~1h)."""
        url = f"{self._host}/api/authorize/login"
        payload = {
            "username": self._username,
            "password": self._password,
            "remember": False,
            "expires": 3600000,
            "verifyCode": "",
            "verifyKey": "",
        }
        try:
            async with self._session.post(url, json=payload, timeout=_TIMEOUT) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise NakedConnectionError(f"Login request failed: {err}") from err

        token = ((data or {}).get("result") or {}).get("token")
        if not token:
            raise NakedAuthError(f"No token in login response: {data}")
        self._token = token
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: aiohttp.ClientTimeout = _TIMEOUT,
    ) -> tuple[int, Any]:
        """Authenticated request. Re-logs in once if the token is rejected."""
        for attempt in (1, 2):
            if not self._token:
                await self.async_login()
            headers = {"X-Access-Token": self._token or ""}
            try:
                async with self._session.request(
                    method,
                    f"{self._host}{path}",
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    if resp.status in (401, 403) and attempt == 1:
                        self._token = None
                        continue
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = None
                    return resp.status, data
            except (aiohttp.ClientError, TimeoutError) as err:
                raise NakedConnectionError(f"{method} {path} failed: {err}") from err
        raise NakedAuthError("Token rejected after re-login")

    async def async_push_url(self) -> tuple[str, str]:
        """Websocket URL for the cloud's live message channel, with a fresh token."""
        token = await self.async_login()
        base = self._host.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/api/messaging/{token}?:X_Access_Token={token}", token

    # ------------------------------------------------------------ readings

    async def async_get_properties(self) -> dict[str, dict[str, Any]]:
        """Return {property_id: item} of the device's latest reported values."""
        status, data = await self._request(
            "GET", f"/api/device-instance/{self._device_id}/properties/latest"
        )
        result = (data or {}).get("result") if isinstance(data, dict) else None
        if status != 200 or not isinstance(result, list):
            raise NakedConnectionError(f"Unexpected properties payload [{status}]: {data}")
        return {
            item["property"]: item
            for item in result
            if isinstance(item, dict) and "property" in item
        }

    # -------------------------------------------------------------- sockets

    async def async_get_socket_reports(self, deep: bool = False) -> dict[str, dict[str, Any]]:
        """Newest hub-reported state per SOCKETn, read from the device message log.

        Socket state is not part of properties/latest, but every change the
        hub confirms is logged as a REPORT_PROPERTY message. Returns
        {"SOCKET2": {"ts": <ms>, "fields": {"name": "Pool_Light", "SW": "ON", ...}}}.

        deep=True scans much further back; used on first start so sockets that
        haven't changed recently are still found.
        """
        # Try a server-side filter first so temperature chatter doesn't push
        # socket reports out of the page; fall back to a wide unfiltered page.
        filtered = {
            "pageIndex": 0,
            "pageSize": 100,
            "sorts[0].name": "timestamp",
            "sorts[0].order": "desc",
            "terms[0].column": "content",
            "terms[0].termType": "like",
            "terms[0].value": "%SOCKET%",
        }
        rows = await self._log_rows(filtered)
        found = self._parse_socket_reports(rows)
        if not found:
            unfiltered = {
                "pageIndex": 0,
                "pageSize": 3000 if deep else 500,
                "sorts[0].name": "timestamp",
                "sorts[0].order": "desc",
            }
            found = self._parse_socket_reports(await self._log_rows(unfiltered))
        return found

    async def async_get_recent_writes(self) -> list[tuple[int, dict[str, Any]]]:
        """Settings recently written to the device, by the app or by Home Assistant.

        The device doesn't report some settings back (winter/spa switch, pH dosing),
        so the cloud's record of writes is the only way to see an app-side change.
        Returns [(timestamp_ms, {prop: value})], newest first.
        """
        rows = await self._log_rows({
            "pageIndex": 0,
            "pageSize": 30,
            "sorts[0].name": "timestamp",
            "sorts[0].order": "desc",
            "terms[0].column": "content",
            "terms[0].termType": "like",
            "terms[0].value": "%WRITE_PROPERTY%",
        })
        writes: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            content = row.get("content") if isinstance(row, dict) else None
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    continue
            if not isinstance(content, dict) or content.get("messageType") != "WRITE_PROPERTY":
                continue
            props = content.get("properties")
            if isinstance(props, dict) and props:
                writes.append((int(row.get("createTime") or row.get("timestamp") or 0), props))
        writes.sort(key=lambda w: w[0], reverse=True)
        return writes

    async def async_get_salt_test(self, deep: bool = False) -> dict[str, Any] | None:
        """Result of the most recent salt test, as the unit shows it at the end of the test.

        The test runs on the unit's salt test screen (menu M10), which sends
        "TDS=<ppm>&condition=OK" while it measures. Returns {"value": ppm, "ts": ms}.
        """
        rows = await self._log_rows({
            "pageIndex": 0,
            "pageSize": 200 if deep else 40,
            "sorts[0].name": "timestamp",
            "sorts[0].order": "desc",
            "terms[0].column": "content",
            "terms[0].termType": "like",
            "terms[0].value": "%TDS=%",
        })
        return self._parse_salt_test(rows)

    @staticmethod
    def _parse_salt_test(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The most recent test in the log: median of its readings, with its verdict."""
        readings: list[tuple[int, int, str]] = []  # (ts, ppm, verdict)
        for row in rows:
            content = row.get("content") if isinstance(row, dict) else None
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    continue
            if not isinstance(content, dict) or content.get("event") != "menuStatus":
                continue
            data = content.get("data") or {}
            if data.get("menu") != "M10" or not isinstance(data.get("param"), str):
                continue
            reading = parse_reading(data["param"])
            if reading is None:
                continue
            ts = int(row.get("createTime") or row.get("timestamp") or 0)
            readings.append((ts, *reading))
        if not readings:
            return None
        readings.sort(reverse=True)
        session = [readings[0]]
        for item in readings[1:]:
            if session[-1][0] - item[0] > SESSION_GAP_MS:
                break  # an earlier test
            session.append(item)
        result = summarise([(v, c) for _, v, c in session])
        return {**result, "ts": session[0][0]} if result else None

    async def _log_rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        status, data = await self._request(
            "GET", f"/api/device-instance/{self._device_id}/logs", params=params
        )
        if status != 200 or not isinstance(data, dict):
            return []
        result = data.get("result")
        rows = result.get("data") if isinstance(result, dict) else result
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _parse_socket_reports(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    continue
            if not isinstance(content, dict):
                continue
            if content.get("messageType") != "REPORT_PROPERTY":
                continue
            ts = row.get("timestamp") or content.get("timestamp") or 0
            for key, value in (content.get("properties") or {}).items():
                sock = str(key).upper()
                if sock not in SOCKET_KEYS or not isinstance(value, str):
                    continue
                prev = found.get(sock)
                if prev is None or ts > prev["ts"]:
                    found[sock] = {"ts": ts, "fields": dict(parse_qsl(value))}
        return found

    async def async_set_socket(self, socket: str, mode: str) -> None:
        """Set a socket to ON, OFF or AUTO (the same write the Naked Connect app sends)."""
        socket = socket.upper()
        mode = mode.upper()
        if socket not in SOCKET_KEYS or mode not in SOCKET_MODES:
            raise ValueError(f"Invalid socket/mode: {socket} {mode}")
        await self.async_write_properties({socket: f"SW={mode}"})

    async def async_write_properties(self, values: dict[str, Any]) -> None:
        """Write one or more device settings (same as the Naked Connect app)."""
        status, data = await self._request(
            "PUT",
            f"/api/device-instance/{self._device_id}/property",
            json_body=values,
        )
        if status != 200 or (isinstance(data, dict) and data.get("status") not in (None, 200)):
            raise NakedConnectionError(f"Write {values} rejected [{status}]: {data}")

    async def async_invoke(
        self, function: str, inputs: dict[str, Any] | None = None, *, wait: bool = True
    ) -> Any:
        """Call a device function, e.g. triggerReport or optionRequest (boost, salt test).

        Some on-screen buttons (boost start/stop) are carried out by the device but
        never get a function reply, so the cloud answers "time_out" after 10s. With
        wait=False the command is sent, and a missing reply is not treated as an error;
        the device's own status reports confirm what happened.
        """
        path = f"/api/device/invoked/{self._device_id}/function/{function}"
        if not wait:
            try:
                status, data = await self._request(
                    "POST", path, json_body=inputs or {}, timeout=_SEND_TIMEOUT
                )
            except NakedConnectionError as err:
                if isinstance(err.__cause__, TimeoutError):
                    return None  # sent; the device doesn't reply to this one
                raise
            if isinstance(data, dict) and data.get("code") == "time_out":
                return None
        else:
            status, data = await self._request("POST", path, json_body=inputs or {})
        if status != 200 or (isinstance(data, dict) and data.get("status") not in (None, 200)):
            raise NakedConnectionError(f"Function {function} rejected [{status}]: {data}")
        return (data or {}).get("result") if isinstance(data, dict) else data
