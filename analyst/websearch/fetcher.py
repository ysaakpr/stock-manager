"""The M17 Commons fetcher on Bedrock: a bounded tool loop over our own web search and fetch.

`BedrockWebFetcher` is an `analyst.commons.Fetcher`, the drop-in for `ClaudeWebFetcher` when the
desk runs on Bedrock (pre-registration Amendment 5). The manager's protocol does not change: a
manager still names a query or a URL in its research round, under the roster's rails
(`max_queries` per round, `max_research_rounds`, `max_isins`), and the runtime still fulfils each
one through `SnapshotStore.get_or_fetch`, cache first. What changes is who does the retrieving.
Where the Claude CLI ran `claude -p --tools WebSearch,WebFetch`, this runs one
`BedrockLLM.run_tools` loop per request, offering `WEB_SEARCH` / `WEB_FETCH` backed by
`WebResearch` (Brave / Tavily / Firecrawl, one vendor per call, at most one fallback), and the model
answers with the same pages object the CLI fetcher's `--json-schema` asks for. The pages become
the Commons snapshot the manager cites as `[S:<id>]`, exactly as before.

**Bounds per request** (so the loop can never widen what the rails allow):

- a query: at most `QUERY_BUDGET.searches` (1) search and `QUERY_BUDGET.fetches` fetches, in at
  most `QUERY_BUDGET.rounds` model calls. A manager's round of `max_queries` (8) queries is
  therefore at most 8 searches;
- a URL: no search, one fetch of exactly that URL (no link following), at most
  `URL_BUDGET.rounds` model calls. A URL the guard refuses (a private or metadata address, a
  scheduler-owned host such as nseindia.com) is refused before any model call.

A call over its budget is answered with an error result, never run; the router's own per-session
and monthly caps still apply underneath. The loop's round bound is hard (`ToolLoopExhaustedError`).

**Which path ran** is recorded on every snapshot as its `fetcher`: `bedrock+websearch:<model>` when
the tool loop answered, or the backup's own name (`claude_cli:<model>`) when Bedrock could not serve
the call (`LLMUnavailableError`: unreachable, throttled after retries, 5xx, a rejected or missing
key) and the Claude CLI's web tools ran instead. A request Bedrock *refused* (a 400, a refusal, an
answer that did not parse, a loop that ran out of rounds) is never re-asked on the CLI: it fails
as a `FetchError` and is journaled as unfulfilled, as the decision path's fallback rule has it.

**Two stores.** The tool results are themselves Commons snapshots (`WebResearch` stores every
vendor answer), kept in their own `SnapshotStore` beside the desk's (`web_store_root`). They must
not share one: the desk's store holds a request's lock while this fetcher runs, and a model that
searches the very query it was asked would wait on that lock for ever.

**Cost.** Every loop's summed usage (one exhausted loop included) is handed to ``meter`` — the
desk wires it to `token_usage` under purpose `m17_fetch` — and travels back on the response, where
the runtime journals it as the round's `fetch_tokens_in` / `fetch_tokens_out`.

What this never does: offer a tool other than the two, let a model fetch a URL the guard refuses,
call a vendor beyond a request's budget, fall back on anything but an unavailable Bedrock, or put
a key anywhere a model, a log or a snapshot can see it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from analyst.commons import (
    MAX_PAGE_BYTES,
    MAX_PAGES_PER_REQUEST,
    FetchedPage,
    Fetcher,
    FetchError,
    FetchKind,
    FetchRequest,
    FetchResponse,
)
from analyst.llm import (
    LLMError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
)
from analyst.llm.bedrock import (
    BEDROCK_PROVIDER,
    ToolExecutor,
    ToolLoopExhaustedError,
    ToolLoopResult,
    ToolOutcome,
)
from analyst.websearch.guard import UrlRefusedError
from analyst.websearch.research import WebResearch
from analyst.websearch.tools import UNTRUSTED_CONTENT_RULE, WEB_FETCH, WEB_SEARCH, WebTools
from dataplatform.logging import get_logger
from dataplatform.redaction import mask_secrets

__all__ = [
    "FETCHER_PREFIX",
    "PAGES_OUTPUT",
    "QUERY_BUDGET",
    "URL_BUDGET",
    "BedrockWebFetcher",
    "RequestBudget",
    "ToolLoopModel",
    "web_store_root",
]

_LOG = get_logger(__name__)

#: A snapshot whose `fetcher` starts with this was retrieved by this module's tool loop.
FETCHER_PREFIX: Final[str] = "bedrock+websearch"


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """What one Commons request may spend: vendor searches, vendor fetches, model calls."""

    searches: int
    fetches: int
    rounds: int

    def __post_init__(self) -> None:
        if self.searches < 0 or self.fetches < 0:
            raise ValueError("a request budget cannot be negative")
        if self.searches + self.fetches < 1:
            raise ValueError("a request budget needs at least one search or fetch")
        if self.rounds < 2:
            raise ValueError("a tool loop needs at least two rounds: one to call, one to answer")


#: A query: one search, then up to two of its hits read in full; search, fetch, answer, plus one
#: round of slack for a fetch that failed and is replaced. Two fetches, not the CLI's three, because
#: the session's fetch cap equals its search cap (`WEBSEARCH_SESSION_MAX_*`).
QUERY_BUDGET: Final[RequestBudget] = RequestBudget(searches=1, fetches=2, rounds=4)
#: A URL: that page and nothing else; fetch, answer, plus one round of slack.
URL_BUDGET: Final[RequestBudget] = RequestBudget(searches=0, fetches=1, rounds=3)

#: The answer the loop must end with: the same pages object `ClaudeWebFetcher`'s `--json-schema`
#: asks for, so a snapshot is shaped the same whichever path produced it.
PAGES_OUTPUT: Final[ToolSpec] = ToolSpec(
    name="pages",
    description=(
        f"The transcribed pages: at most {MAX_PAGES_PER_REQUEST}, each with the page's URL, its "
        f"title and its factual text (about {MAX_PAGE_BYTES // 1024} KB at most)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "maxItems": MAX_PAGES_PER_REQUEST,
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["url", "title", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["pages"],
        "additionalProperties": False,
    },
)

_SYSTEM_PROMPT: Final[str] = (
    "You are a retrieval and transcription tool. You have exactly two tools, web_search and "
    "web_fetch, each with a small fixed budget for this request. Use them to retrieve what the "
    "user names, then transcribe what the pages say.\n"
    "Rules:\n"
    "- Transcribe; never judge. Do not summarise opinions as yours, rank, recommend, predict, "
    "score, or say whether anything is a good or bad investment.\n"
    "- Copy the page's factual text as written: figures, dates, names, tables as plain text. "
    "Drop navigation, adverts and cookie banners.\n"
    "- Never invent content. If a page could not be retrieved, leave it out. A search result's "
    "snippet may be transcribed as a page of its own when its page could not be fetched.\n"
    f"- Return at most {MAX_PAGES_PER_REQUEST} pages and at most about {MAX_PAGE_BYTES // 1024} "
    "KB of text per page.\n"
    f"- {UNTRUSTED_CONTENT_RULE}\n"
    "Answer only through the structured output, as soon as your budget is spent."
)


def _prompt(request: FetchRequest, budget: RequestBudget) -> str:
    if request.kind is FetchKind.QUERY:
        reads = (
            f"Then fetch up to {budget.fetches} of the most relevant results with web_fetch and "
            "transcribe each."
            if budget.fetches
            else "Do not fetch any page: transcribe the search results themselves."
        )
        return f"Run one web search (web_search) for: {request.target}\n{reads}"
    return (
        "Fetch exactly this URL with web_fetch and transcribe it. Do not search or follow links: "
        f"{request.target}"
    )


def web_store_root(fetch_root: Path) -> Path:
    """Where the tool results' own snapshots live: beside the desk's store, never inside it."""
    return fetch_root.parent / "websearch"


class ToolLoopModel(Protocol):
    """The slice of `analyst.llm.BedrockLLM` this module needs: its client-side tool loop."""

    def run_tools(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        system: str | None = None,
        max_rounds: int = ...,
        output: ToolSpec | None = None,
    ) -> ToolLoopResult: ...


#: Receives each tool loop's billed usage, as a response (`provider`, Bedrock `model` id, tokens).
Meter = Callable[[LLMResponse], None]


class _RequestTools:
    """One request's executor: the budget, the URL pin, then `WebTools`. Not shared across
    requests, so it needs no lock even though the runtime fetches on several threads."""

    def __init__(self, tools: WebTools, request: FetchRequest, budget: RequestBudget) -> None:
        self._tools = tools
        self._request = request
        self._budget = budget
        self.searches = 0
        self.fetches = 0
        self.snapshots: list[str] = []
        self.refused = 0

    def __call__(self, call: ToolCall) -> ToolOutcome:
        if call.name == WEB_SEARCH.name:
            if self.searches >= self._budget.searches:
                return self._refuse(
                    f"web_search refused: this request's budget is {self._budget.searches} "
                    "search(es) and it is spent; answer from what you have"
                )
            self.searches += 1
        elif call.name == WEB_FETCH.name:
            if self.fetches >= self._budget.fetches:
                return self._refuse(
                    f"web_fetch refused: this request's budget is {self._budget.fetches} "
                    "fetch(es) and it is spent; answer from what you have"
                )
            if self._request.kind is FetchKind.URL and not self._is_target(call):
                return self._refuse(
                    f"web_fetch refused: this request fetches exactly {self._request.target} and "
                    "nothing else"
                )
            self.fetches += 1
        result = self._tools.handle(call.name, call.arguments)
        if result.snapshot is not None:
            self.snapshots.append(result.snapshot.id)
        return ToolOutcome(result.content, is_error=result.is_error)

    def _is_target(self, call: ToolCall) -> bool:
        url = call.arguments.get("url")
        if not isinstance(url, str):
            return False
        try:
            return FetchRequest.url(url, self._request.session).target == self._request.target
        except ValueError:
            return False

    def _refuse(self, reason: str) -> ToolOutcome:
        self.refused += 1
        return ToolOutcome(reason, is_error=True)


class BedrockWebFetcher:
    """The live `Fetcher` on Bedrock: one bounded tool loop over `WEB_SEARCH` / `WEB_FETCH` per
    request, with the Claude CLI's web fetcher as the backup when Bedrock is unavailable.

    What it does: see the module docstring. ``name`` is read by `SnapshotStore` after `fetch`
    returns, on the same thread, so it is kept thread-locally (the runtime fetches on up to three
    threads through one fetcher).
    What it assumes: ``research`` stores into its own `SnapshotStore`, not the one this fetcher is
    handed to (`web_store_root`).
    What it never does: exceed a request's budget, fall back on a refusal, or retry.
    """

    def __init__(
        self,
        model: ToolLoopModel,
        research: WebResearch,
        *,
        model_id: str,
        backup: Fetcher | None = None,
        meter: Meter | None = None,
        bedrock_model_id: Callable[[str], str] | None = None,
        query_budget: RequestBudget = QUERY_BUDGET,
        url_budget: RequestBudget = URL_BUDGET,
    ) -> None:
        if not model_id.strip():
            raise ValueError("the fetcher needs a model id")
        self._model = model
        self._research = research
        self._model_id = model_id
        self._backup = backup
        self._meter = meter
        self._bedrock_model_id = bedrock_model_id or (lambda name: name)
        self._budgets = {FetchKind.QUERY: query_budget, FetchKind.URL: url_budget}
        self._local = threading.local()

    @property
    def name(self) -> str:
        served = getattr(self._local, "served", None)
        return served if isinstance(served, str) else f"{FETCHER_PREFIX}:{self._model_id}"

    @property
    def research(self) -> WebResearch:
        return self._research

    def fetch(self, request: FetchRequest) -> FetchResponse:
        """Retrieve and transcribe `request`. Raises `FetchError` on any failure or refusal."""
        self._local.served = None
        if request.kind is FetchKind.URL:
            # Refused before a model is asked: a call it could only be told no about costs money.
            try:
                self._research.guard.check(request.target)
            except UrlRefusedError as exc:
                raise FetchError(f"refused: {exc.reason}") from exc
        budget = self._budgets[request.kind]
        executor = _RequestTools(WebTools(self._research, session=request.session), request, budget)
        offered = tuple(
            spec
            for spec, allowed in ((WEB_SEARCH, budget.searches), (WEB_FETCH, budget.fetches))
            if allowed
        )
        try:
            result = self._model.run_tools(
                [Message(Role.USER, _prompt(request, budget))],
                model=self._model_id,
                tools=offered,
                execute=executor,
                system=_SYSTEM_PROMPT,
                max_rounds=budget.rounds,
                output=PAGES_OUTPUT,
            )
        except ToolLoopExhaustedError as exc:
            self._charge(
                LLMResponse(
                    provider=BEDROCK_PROVIDER,
                    model=self._bedrock_model_id(self._model_id),
                    text="",
                    usage=exc.usage,
                    stop_reason=StopReason.TOOL_USE,
                )
            )
            self._log(request, executor, path="bedrock", outcome="exhausted", rounds=exc.rounds)
            raise FetchError(
                f"the research loop ran out of its {budget.rounds} model calls without an answer"
            ) from exc
        except LLMUnavailableError as exc:
            return self._fall_back(request, executor, exc)
        except LLMError as exc:
            self._log(request, executor, path="bedrock", outcome="refused", rounds=None)
            raise FetchError(f"the research model failed: {mask_secrets(str(exc))[:400]}") from exc
        self._charge(result.response)
        pages = _pages(result.response)
        self._local.served = f"{FETCHER_PREFIX}:{self._model_id}"
        self._log(request, executor, path="bedrock", outcome="answered", rounds=result.rounds)
        return FetchResponse(pages=pages, usage=result.response.usage)

    def _fall_back(
        self, request: FetchRequest, executor: _RequestTools, exc: LLMUnavailableError
    ) -> FetchResponse:
        reason = exc.reason.value
        if self._backup is None:
            self._log(request, executor, path="bedrock", outcome="unavailable", rounds=None)
            raise FetchError(
                f"Bedrock could not serve the research call ({reason}) and no backup is set"
            ) from exc
        _LOG.warning(
            "commons.fetch.backup",
            key=request.key,
            kind=request.kind.value,
            session=request.session.isoformat(),
            primary=BEDROCK_PROVIDER,
            backup=self._backup.name,
            reason=reason,
            detail=mask_secrets(str(exc))[:400],
            searches_before=executor.searches,
            fetches_before=executor.fetches,
        )
        response = self._backup.fetch(request)
        self._local.served = self._backup.name
        self._log(request, executor, path="backup", outcome="answered", rounds=None)
        return response

    def _charge(self, response: LLMResponse) -> None:
        if self._meter is None:
            return
        try:
            self._meter(response)
        except Exception as exc:
            # The call was billed; a ledger that cannot take the row is a defect to see, and the
            # research is withheld rather than shown unmetered.
            _LOG.error(
                "commons.fetch.meter_failed",
                model=response.model,
                error=mask_secrets(f"{type(exc).__name__}: {exc}")[:400],
            )
            raise FetchError("the research call's cost could not be recorded") from exc

    def _log(
        self,
        request: FetchRequest,
        executor: _RequestTools,
        *,
        path: str,
        outcome: str,
        rounds: int | None,
    ) -> None:
        _LOG.info(
            "commons.fetch.web_research",
            key=request.key,
            kind=request.kind.value,
            session=request.session.isoformat(),
            path=path,
            outcome=outcome,
            model=self._model_id,
            rounds=rounds,
            searches=executor.searches,
            fetches=executor.fetches,
            refused_calls=executor.refused,
            tool_snapshots=executor.snapshots,
        )


def _pages(response: LLMResponse) -> tuple[FetchedPage, ...]:
    """The structured answer's pages. Raises `FetchError` on anything but the pages object."""
    calls = [c for c in response.tool_calls if c.name == PAGES_OUTPUT.name]
    if len(calls) != 1:
        raise FetchError("the research model returned no structured pages")
    raw: Any = calls[0].arguments.get("pages")
    if not isinstance(raw, list):
        raise FetchError("the research model returned no structured pages")
    pages: list[FetchedPage] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("url"), str)
            or not isinstance(item.get("text"), str)
        ):
            raise FetchError("the research model returned a page without a URL and text")
        title = item.get("title")
        pages.append(
            FetchedPage(
                url=item["url"], text=item["text"], title=title if isinstance(title, str) else ""
            )
        )
    return tuple(pages)
