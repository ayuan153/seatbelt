"""Ingress adapters — translate a provider's wire protocol to/from Agentbelt's normalized model.

The guard pipeline (scope/risk/budget/PDP/provenance/egress/telemetry, in app.py) is
protocol-agnostic: it operates on the normalized `TurnRequest` / `TurnUpstream` below and calls back
into an adapter to serialize each outcome. Adding a new agent runtime (e.g. the Anthropic Messages
API for Claude Code) means adding an adapter here — the guards never change. See
docs/design/agent-integration.md and ADR-0001.

This module currently provides the OpenAI Chat Completions adapter (the original, unchanged
behavior). It is a pure structural extraction from app.py.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse

# Returned to the caller when every tool call in a turn is denied (capability-downgrade).
_BLOCKED_ACTION_MSG = "I'm not able to complete that action."


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# --- normalized model shared by all adapters --------------------------------


@dataclass
class TurnRequest:
    """A provider request parsed into the fields the guard pipeline needs."""
    messages: list  # list[Message] — for scope/risk evaluation
    raw_messages: list  # original provider message dicts — for provenance hashing
    tools: list  # OpenAI-style tool defs (name + annotations + x_mcp_server) for tiering
    upstream_body: dict  # body to forward to the upstream model


@dataclass
class TurnUpstream:
    """An upstream model response normalized for the guard pipeline."""
    content: str
    tool_calls: list
    prompt_tokens: int | None
    completion_tokens: int | None
    raw_message: dict = field(default_factory=dict)  # provider message object (for tool forwarding)
    raw_resp: dict = field(default_factory=dict)  # full provider response (for tool forwarding)


# --- OpenAI Chat Completions adapter ----------------------------------------


def _completion(content: str, usage: dict | None = None) -> dict:
    return {
        "id": f"agentbelt-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class ChatCompletionsAdapter:
    """OpenAI-compatible /v1/chat/completions ingress (the original wire protocol)."""

    name = "chat_completions"

    def parse_request(self, body: dict, message_cls) -> TurnRequest:
        raw_messages = body.get("messages", []) or []
        messages = [message_cls(m.get("role", ""), m.get("content", "") or "") for m in raw_messages]
        return TurnRequest(messages=messages, raw_messages=raw_messages,
                           tools=body.get("tools", []) or [], upstream_body=body)

    def call_upstream(self, upstream, req: TurnRequest) -> TurnUpstream:
        resp = upstream(req.upstream_body)
        message = (resp.get("choices", [{}])[0].get("message", {})) or {}
        usage = resp.get("usage") or {}
        return TurnUpstream(
            content=message.get("content", "") or "",
            tool_calls=message.get("tool_calls") or [],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw_message=message, raw_resp=resp)

    # --- outcome serializers (one per guard-pipeline decision) ---

    def throttle(self, reason: str) -> JSONResponse:
        return JSONResponse(status_code=429, content={"error": {"message": reason, "type": "rate_limit"}})

    def deflect(self, text: str) -> JSONResponse:
        return JSONResponse(content=_completion(text))

    def blocked_action(self) -> JSONResponse:
        return JSONResponse(content=_completion(_BLOCKED_ACTION_MSG))

    def forward_tools(self, up: TurnUpstream, kept: list) -> JSONResponse:
        up.raw_message["tool_calls"] = kept  # forward upstream resp with denied calls stripped
        return JSONResponse(content=up.raw_resp)

    def answer(self, content: str, in_tok: int, out_tok: int) -> JSONResponse:
        return JSONResponse(content=_completion(content, {
            "prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok}))
