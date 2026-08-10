# Frozen Anthropic Messages API responses (era: 2026-06)

Recorded response *shapes* for `POST /v1/messages`, as the SDK's `anthropic.types.Message`
deserializes them. `tests/unit/test_llm_accounting.py` serves these through an
`httpx.MockTransport`, so `AnthropicLLM.complete` is exercised end to end without a socket and
without a credential (B4, B8) — the test suite never reaches the network.

These are **hand-authored to the published response schema**, not captured from a live call: no
Anthropic key exists on this machine, so nothing here has been recorded from the real API. They
are therefore evidence about *this client's* parsing and about the request it builds, not
evidence about what the provider returns. The day a credential lands, re-record them from a real
call and keep the same file names — a recorder must strip `x-api-key` and every other
authentication header before writing here (AGENTIC_CONTEXT §6 invariant #13).

Nothing in this directory contains a credential, and nothing may. The `id` fields are literal
`msg_*` identifiers from no real account.

| File | Covers |
|---|---|
| `end_turn.json` | A complete answer, with all four token buckets non-zero and distinct. |
| `tool_use.json` | A `tool_use` stop, with text and a tool call in one response. |
| `max_tokens.json` | A truncated answer — billed, and `truncated` must be visible to the caller. |
| `refusal.json` | A refusal: HTTP 200, empty content, `stop_details.category`. |
| `context_window_exceeded.json` | A stop reason this client does not map, which must fail loud. |
| `resolved_model_id.json` | An alias resolved to a dated snapshot id — what gets priced. |
