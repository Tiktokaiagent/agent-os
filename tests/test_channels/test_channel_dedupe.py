from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.msteams import MSTeamsChannel, MSTeamsChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.types import IncomingMessage


class _BodyRequest:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_slack_webhook_dedupes_retried_event_callback() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C1", signing_secret="test-secret")

    payload = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "text": "draw an image",
            "ts": "1710000000.000100",
            "channel_type": "im",
        },
    }

    # Sign requests so signature verification passes
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{json.dumps(payload)}"
    signature = "v0=" + hmac.HMAC(
        b"test-secret", sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    async def _signed_request() -> _BodyRequest:
        req = _BodyRequest(payload)
        req.headers["X-Slack-Request-Timestamp"] = timestamp
        req.headers["X-Slack-Signature"] = signature
        return req

    await channel._handle_webhook(await _signed_request())  # noqa: SLF001
    await channel._handle_webhook(await _signed_request())  # noqa: SLF001

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "draw an image"


@pytest.mark.asyncio
async def test_msteams_enqueue_dedupes_retried_activity_id() -> None:
    channel = MSTeamsChannel(MSTeamsChannelConfig())
    msg = IncomingMessage(
        sender_id="u1",
        channel_id="conv1",
        content="make a deck",
        metadata={"activity_id": "activity-1"},
    )

    channel.enqueue(msg)
    channel.enqueue(msg)

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "make a deck"


@pytest.mark.asyncio
async def test_discord_gateway_dedupes_replayed_message_create() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    payload = {
        "id": "message-1",
        "channel_id": "channel-1",
        "content": "hello",
        "author": {"id": "user-1"},
        "mentions": [],
        "attachments": [],
    }

    await channel._handle_dispatch("MESSAGE_CREATE", payload)  # noqa: SLF001
    await channel._handle_dispatch("MESSAGE_CREATE", payload)  # noqa: SLF001

    assert channel._queue.qsize() == 1  # noqa: SLF001
    assert (await channel.receive()).content == "hello"
