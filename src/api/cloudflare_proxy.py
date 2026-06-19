"""Thin HTTP wrapper around the shared Cloudflare Worker proxy.

The Worker is the same one used by macOS Clicky; it forwards requests to
Anthropic, ElevenLabs, and AssemblyAI without exposing API keys to the client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import requests

from ..utils.constants import (
    WORKER_CHAT_PATH,
    WORKER_TRANSCRIBE_TOKEN_PATH,
    WORKER_TTS_PATH,
)
from ..utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class SSEEvent:
    """One server-sent event parsed from the Worker's `/chat` response.

    The Worker forwards Anthropic's stream verbatim, so events come through
    looking like:

        event: content_block_delta
        data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}

    Multiple `data:` lines per event are concatenated with newlines per the
    SSE spec; a blank line separates events.
    """
    event: str
    data: str


class CloudflareProxy:
    """Single transport for all Worker-mediated endpoints."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        # Normalize a missing scheme. Users often paste the bare hostname
        # (e.g. `name.workers.dev`); without a scheme `requests` raises
        # `MissingSchema`. Cloudflare Workers always speak HTTPS.
        normalized = base_url.strip()
        if normalized and "://" not in normalized:
            normalized = f"https://{normalized}"
        self.base_url = normalized.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    # ---- /chat ----
    def chat(self, payload: dict[str, Any], stream: bool = False) -> requests.Response:
        url = f"{self.base_url}{WORKER_CHAT_PATH}"
        log.debug("POST %s stream=%s", url, stream)
        response = self._session.post(url, json=payload, stream=stream, timeout=self.timeout)
        # Surface the upstream error body so the user sees the real problem
        # (e.g. "Your credit balance is too low" from Anthropic) instead of
        # an opaque "400 Bad Request". Skip for streaming responses — the
        # body iterator is the caller's responsibility there.
        if not stream and not response.ok:
            raise _build_http_error("/chat", response)
        return response

    def chat_stream(self, payload: dict[str, Any]) -> Iterator[SSEEvent]:
        """Yield parsed `SSEEvent`s from the Worker's `/chat` SSE response.

        We accumulate lines until a blank separator, then emit one event with
        the joined `data:` payload. This handles multi-line `data:` (which
        Anthropic doesn't currently emit, but the SSE spec allows) and skips
        comment lines (`:` prefix) that some intermediaries inject as
        keepalives.
        """
        response = self.chat({**payload, "stream": True}, stream=True)
        if not response.ok:
            raise _build_http_error("/chat", response)

        current_event = "message"
        data_lines: list[str] = []

        for raw_line in response.iter_lines(decode_unicode=True):
            # `iter_lines` yields the chunk between newlines without the
            # newline. An empty string therefore signals the end-of-event
            # blank separator.
            if raw_line is None:
                continue
            if raw_line == "":
                if data_lines:
                    yield SSEEvent(event=current_event, data="\n".join(data_lines))
                    data_lines = []
                    current_event = "message"
                continue
            if raw_line.startswith(":"):
                # Comment / keepalive line; ignore.
                continue
            if raw_line.startswith("event:"):
                current_event = raw_line[len("event:"):].strip()
                continue
            if raw_line.startswith("data:"):
                # SSE spec: strip exactly one leading space if present.
                payload_segment = raw_line[len("data:"):]
                if payload_segment.startswith(" "):
                    payload_segment = payload_segment[1:]
                data_lines.append(payload_segment)
                continue
            # Unknown field; ignore per SSE spec.

        # Flush trailing event if the server closed without a final blank line.
        if data_lines:
            yield SSEEvent(event=current_event, data="\n".join(data_lines))

    # ---- /tts ----
    def tts(self, text: str) -> bytes:
        """Synthesize speech.

        Note: the Worker injects the voice id from its `ELEVENLABS_VOICE_ID`
        secret. The client must NOT send a `voice_id` field — the upstream
        Worker is a pass-through to ElevenLabs and including extra keys would
        leak into the ElevenLabs request body.
        """
        url = f"{self.base_url}{WORKER_TTS_PATH}"
        log.debug("POST %s (%d chars)", url, len(text))
        resp = self._session.post(
            url,
            json={"text": text},
            timeout=self.timeout,
        )
        if not resp.ok:
            raise _build_http_error("/tts", resp)
        return resp.content

    # ---- /transcribe-token ----
    def transcription_token(self) -> str:
        """Short-lived AssemblyAI realtime token (used by realtime providers).

        The upstream Worker rejects any non-POST request at the very top of
        its handler, so this MUST be a POST even though the route is
        semantically a fetch.
        """
        url = f"{self.base_url}{WORKER_TRANSCRIBE_TOKEN_PATH}"
        resp = self._session.post(url, json={}, timeout=self.timeout)
        if not resp.ok:
            raise _build_http_error("/transcribe-token", resp)
        data = resp.json()
        return data.get("token") or data.get("temp_token") or ""

    def close(self) -> None:
        self._session.close()


def _build_http_error(route: str, response: requests.Response) -> RuntimeError:
    """Compose a friendly exception including the upstream response body.

    The Worker passes through upstream API errors verbatim; their bodies
    contain the actionable diagnostic (e.g. "Your credit balance is too
    low" from Anthropic, "Invalid API key" from AssemblyAI). Without
    surfacing the body, the user only sees the HTTP status code and has
    to curl the Worker themselves to figure out what went wrong.
    """
    body_preview = (response.text or "").strip()
    if len(body_preview) > 500:
        body_preview = body_preview[:500] + "..."
    if not body_preview:
        body_preview = "(empty response body)"
    return RuntimeError(
        f"Worker {route} returned {response.status_code}: {body_preview}"
    )
