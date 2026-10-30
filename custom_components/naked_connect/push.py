"""Live updates from the Naked Connect cloud.

The cloud has a websocket channel that streams every message for a device as it
happens: property reports, settings written by the app or Home Assistant, and
on-screen events such as the salt test result. The Naked Connect app uses the same
channel, which is why it updates instantly. Polling stays on as a fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .api import NakedClient, NakedError

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 5  # seconds
RECONNECT_MAX = 300
# Tokens last about an hour; reconnect with a fresh one before that.
SESSION_MAX = 50 * 60
HEARTBEAT = 30


class NakedPush:
    """Keeps one websocket open per device and hands each message to a callback."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: NakedClient,
        on_message: Callable[[dict[str, Any]], None],
        on_state: Callable[[bool], None],
    ) -> None:
        self._session = session
        self._client = client
        self._on_message = on_message
        self._on_state = on_state
        self._task: asyncio.Task | None = None
        self.connected = False

    def start(self, create_task: Callable[[Awaitable[Any], str], asyncio.Task]) -> None:
        if self._task is None:
            self._task = create_task(self._run(), f"naked push {self._client.device_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._set_connected(False)

    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            self._on_state(value)

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while True:
            try:
                await self._session_once()
                delay = RECONNECT_MIN  # a session that ended normally: reconnect promptly
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, NakedError, ValueError) as err:
                _LOGGER.debug("Live channel for %s dropped: %s", self._client.device_id, err)
                delay = min(delay * 2, RECONNECT_MAX)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error on the Naked Connect live channel")
                delay = min(delay * 2, RECONNECT_MAX)
            self._set_connected(False)
            await asyncio.sleep(delay + random.uniform(0, 2))

    async def _session_once(self) -> None:
        url, token = await self._client.async_push_url()
        async with self._session.ws_connect(
            url, heartbeat=HEARTBEAT, timeout=aiohttp.ClientWSTimeout(ws_close=10)
        ) as ws:
            await ws.send_str(json.dumps({
                "type": "sub",
                "id": "naked",
                "topic": f"/device/*/{self._client.device_id}/**",
                "parameter": {},
            }))
            self._set_connected(True)
            _LOGGER.debug("Live channel open for %s", self._client.device_id)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + SESSION_MAX
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return  # refresh the token
                try:
                    msg = await ws.receive(timeout=remaining)
                except TimeoutError:
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if data.get("type") == "result" and isinstance(data.get("payload"), dict):
                        self._on_message(data["payload"])
                    elif data.get("type") == "error":
                        raise NakedError(f"Live channel refused: {data.get('message')}")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise aiohttp.ClientError(f"closed ({msg.type.name})")
