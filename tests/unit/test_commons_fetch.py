"""The Commons fetcher (M17.3, pre-registration §5): one fetch per request and session, kept.

Four properties carry the weight here, and each has a test that fails if the logic is inverted:

1. **Cache first.** A second identical request in one session is answered from the store, and the
   fetcher is not called. A different session is a different request.
2. **Immutable, content-addressed.** A snapshot's file bytes are its canonical bytes, its id is
   their sha256, and the file is never rewritten. A tampered byte fails the digest on every read.
3. **The decision path stays tool-less.** `ClaudeCliLLM` is still invoked with `--tools ""`, and
   only `ClaudeWebFetcher` names a web tool.
4. **Bounded and redacted before storage.** Over-long pages are cut and say so, extra pages are
   dropped and counted, and a credential-shaped string never reaches the disk (invariant #13).

Everything is offline. The fetcher is a stub, `subprocess.run` is replaced wherever the CLI would
run, and every planted credential is assembled at runtime so this file trips no secret scanner.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from analyst.commons import fetch as fetch_module
from analyst.commons.fetch import (
    ALLOWED_WEB_TOOLS,
    MAX_PAGE_BYTES,
    MAX_PAGES_PER_REQUEST,
    ClaudeWebFetcher,
    FetchedPage,
    FetchError,
    FetchKind,
    FetchRequest,
    FetchResponse,
    SnapshotIntegrityError,
    SnapshotStore,
    default_store_root,
    parse_stream,
)
from analyst.commons.fetch_cli import app
from analyst.llm.claude_cli import ClaudeCliLLM
from analyst.llm.cli_env import CLAUDE_CLI_ENV_ALLOWLIST
from analyst.llm.client import Message, Role, Usage
from dataplatform.clock import IST, FrozenClock

REPO = Path(__file__).resolve().parents[2]
SESSION = date(2026, 10, 9)
NEXT_SESSION = date(2026, 10, 12)
AT = datetime(2026, 10, 9, 18, 30, tzinfo=IST)


def _fake(seed: str, length: int) -> str:
    """A deterministic credential-shaped value: never a literal in this file."""
    out = ""
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{len(out)}".encode()).hexdigest()
    return out[:length]


class StubFetcher:
    """A `Fetcher` that answers from a fixed page list and counts how often it was asked."""

    name = "stub"

    def __init__(
        self,
        pages: Sequence[FetchedPage] | None = None,
        *,
        fail: bool = False,
        usage: Usage | None = None,
    ) -> None:
        self.pages = tuple(
            pages
            if pages is not None
            else (FetchedPage(url="https://www.nseindia.com/holidays", text="Diwali: 2026-11-09"),)
        )
        self.fail = fail
        self.usage = usage
        self.calls: list[FetchRequest] = []

    def fetch(self, request: FetchRequest) -> FetchResponse:
        self.calls.append(request)
        if self.fail:
            raise FetchError("stub outage")
        return FetchResponse(pages=self.pages, usage=self.usage)


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(default_store_root(tmp_path), clock=FrozenClock(AT))


# ── 1. a second identical request is a cache hit ────────────────────────────────────────────


def test_a_second_identical_request_in_one_session_never_calls_the_fetcher(
    store: SnapshotStore,
) -> None:
    fetcher = StubFetcher(usage=Usage(input_tokens=10, output_tokens=5))
    request = FetchRequest.query("NSE holiday list 2026", SESSION)

    first = store.get_or_fetch(request, fetcher)
    second = store.get_or_fetch(FetchRequest.query("NSE holiday list 2026", SESSION), fetcher)

    assert len(fetcher.calls) == 1
    assert first.cache_hit is False and second.cache_hit is True
    assert second.snapshot == first.snapshot
    assert first.usage == Usage(input_tokens=10, output_tokens=5)
    assert second.usage is None  # a hit spent nothing


def test_the_cache_survives_a_new_store_instance(tmp_path: Path) -> None:
    """The index is on disk, so another manager's process sees the same hit."""
    root = default_store_root(tmp_path)
    fetcher = StubFetcher()
    request = FetchRequest.url("https://www.nseindia.com/holidays", SESSION)
    first = SnapshotStore(root, clock=FrozenClock(AT)).get_or_fetch(request, fetcher)
    again = SnapshotStore(root, clock=FrozenClock(AT)).get_or_fetch(request, fetcher)
    assert len(fetcher.calls) == 1 and again.cache_hit and again.snapshot.id == first.snapshot.id


def test_two_spellings_of_one_query_share_a_hit(store: SnapshotStore) -> None:
    fetcher = StubFetcher()
    store.get_or_fetch(FetchRequest.query("NSE  Holiday   List 2026", SESSION), fetcher)
    hit = store.get_or_fetch(FetchRequest.query(" nse holiday list 2026 ", SESSION), fetcher)
    assert hit.cache_hit and len(fetcher.calls) == 1


def test_two_spellings_of_one_url_share_a_hit(store: SnapshotStore) -> None:
    fetcher = StubFetcher()
    store.get_or_fetch(
        FetchRequest.url("HTTPS://WWW.NSEINDIA.COM:443/holidays#top", SESSION), fetcher
    )
    hit = store.get_or_fetch(
        FetchRequest.url("https://www.nseindia.com/holidays", SESSION), fetcher
    )
    assert hit.cache_hit and len(fetcher.calls) == 1


def test_another_session_is_another_request(store: SnapshotStore) -> None:
    """Inverted, this would serve last week's page as today's evidence."""
    fetcher = StubFetcher()
    first = store.get_or_fetch(FetchRequest.query("nifty results", SESSION), fetcher)
    later = store.get_or_fetch(FetchRequest.query("nifty results", NEXT_SESSION), fetcher)
    assert later.cache_hit is False and len(fetcher.calls) == 2
    assert later.snapshot.id != first.snapshot.id
    assert later.snapshot.request.session == NEXT_SESSION


def test_a_query_and_a_url_with_the_same_text_are_different_requests(store: SnapshotStore) -> None:
    fetcher = StubFetcher()
    store.get_or_fetch(FetchRequest.query("https://example.org/", SESSION), fetcher)
    other = store.get_or_fetch(FetchRequest.url("https://example.org/", SESSION), fetcher)
    assert other.cache_hit is False and len(fetcher.calls) == 2


def test_a_failed_fetch_stores_nothing_and_the_next_ask_retries(store: SnapshotStore) -> None:
    request = FetchRequest.query("nse circular", SESSION)
    with pytest.raises(FetchError):
        store.get_or_fetch(request, StubFetcher(fail=True))
    assert store.lookup(request) is None
    assert not store.index_path.exists()
    healthy = StubFetcher()
    assert store.get_or_fetch(request, healthy).cache_hit is False
    assert len(healthy.calls) == 1


def test_an_empty_answer_is_a_failure_not_a_snapshot(store: SnapshotStore) -> None:
    request = FetchRequest.query("nothing", SESSION)
    with pytest.raises(FetchError, match="no pages"):
        store.get_or_fetch(request, StubFetcher(pages=()))
    assert store.lookup(request) is None


def test_an_empty_answer_is_logged_as_a_failed_fetch_with_its_reason(store: SnapshotStore) -> None:
    """M17.12: 20 fetches of the 2026-10-09 dry run came back empty and left no log line."""
    request = FetchRequest.url("https://www.nseindia.com/made-up-page", SESSION)
    with capture_logs() as logs, pytest.raises(FetchError):
        store.get_or_fetch(request, StubFetcher(pages=()))
    (line,) = [e for e in logs if e["event"] == "commons.fetch.failed"]
    assert line["key"] == request.key and line["kind"] == "url"
    assert "no pages" in line["reason"]


started = threading.Event()


class _SlowFetcher(StubFetcher):
    """Holds each fetch open long enough for a second asker to queue behind its lock."""

    def fetch(self, request: FetchRequest) -> FetchResponse:
        started.set()
        time.sleep(0.2)
        return super().fetch(request)


def test_two_concurrent_identical_requests_cost_one_fetch(store: SnapshotStore) -> None:
    started.clear()
    fetcher = _SlowFetcher()
    request = FetchRequest.query("leader ltd order book", SESSION)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.get_or_fetch, request, fetcher)
        assert started.wait(5)
        second = pool.submit(store.get_or_fetch, request, fetcher)
        outcomes = [first.result(timeout=10), second.result(timeout=10)]
    assert len(fetcher.calls) == 1
    assert sorted(o.cache_hit for o in outcomes) == [False, True]
    assert outcomes[0].snapshot.id == outcomes[1].snapshot.id
    assert len(store.index_path.read_text().splitlines()) == 1


def test_concurrent_distinct_requests_each_land_one_whole_index_line(store: SnapshotStore) -> None:
    requests = [FetchRequest.query(f"query number {n}", SESSION) for n in range(12)]
    fetcher = StubFetcher()
    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda r: store.get_or_fetch(r, fetcher), requests))
    assert len(fetcher.calls) == 12 and not any(o.cache_hit for o in outcomes)
    lines = store.index_path.read_text().splitlines()
    assert sorted(json.loads(line)["key"] for line in lines) == sorted(r.key for r in requests)
    assert store.verify() == []


# ── 2. immutable and content-addressed ──────────────────────────────────────────────────────


def test_a_snapshot_id_is_the_sha256_of_its_stored_bytes(store: SnapshotStore) -> None:
    snapshot = store.get_or_fetch(FetchRequest.query("q", SESSION), StubFetcher()).snapshot
    data = store.snapshot_path(snapshot.id).read_bytes()
    assert hashlib.sha256(data).hexdigest() == snapshot.id
    assert snapshot.canonical_bytes() == data
    assert snapshot.byte_length == len(data)
    assert snapshot.fetched_at == AT
    assert snapshot.fetcher == "stub"
    assert snapshot.source_urls == ("https://www.nseindia.com/holidays",)
    assert "Diwali: 2026-11-09" in snapshot.text


def test_a_snapshot_file_is_written_read_only(store: SnapshotStore) -> None:
    snapshot = store.get_or_fetch(FetchRequest.query("q", SESSION), StubFetcher()).snapshot
    mode = store.snapshot_path(snapshot.id).stat().st_mode & 0o777
    assert mode & 0o222 == 0


def test_a_snapshots_bytes_never_change_after_write(store: SnapshotStore) -> None:
    """Writing the same snapshot again leaves the file byte-for-byte and inode-for-inode alone."""
    request = FetchRequest.query("q", SESSION)
    response = FetchResponse(pages=StubFetcher().pages)
    first = store.put(request, response, fetcher="stub")
    path = store.snapshot_path(first.id)
    before, inode = path.read_bytes(), path.stat().st_ino
    again = store.put(request, response, fetcher="stub")  # same clock, same content: same id
    assert again.id == first.id
    assert path.read_bytes() == before and path.stat().st_ino == inode
    assert store.lookup(request) == first


def test_a_tampered_file_fails_its_digest_check(store: SnapshotStore) -> None:
    request = FetchRequest.query("q", SESSION)
    snapshot = store.get_or_fetch(request, StubFetcher()).snapshot
    path = store.snapshot_path(snapshot.id)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"2026-11-09", b"2026-11-10"))

    with pytest.raises(SnapshotIntegrityError, match="digest"):
        store.get(snapshot.id)
    with pytest.raises(SnapshotIntegrityError):
        store.lookup(request)
    fetcher = StubFetcher()
    with pytest.raises(SnapshotIntegrityError):
        store.get_or_fetch(request, fetcher)  # a tampered hit is refused, never silently refetched
    assert fetcher.calls == []
    assert any(snapshot.id in problem for problem in store.verify())


def test_an_untampered_store_verifies_clean(store: SnapshotStore) -> None:
    store.get_or_fetch(FetchRequest.query("a", SESSION), StubFetcher())
    store.get_or_fetch(FetchRequest.url("https://example.org/x", SESSION), StubFetcher())
    assert store.verify() == []


def test_an_index_entry_pointing_at_another_requests_snapshot_is_refused(
    store: SnapshotStore,
) -> None:
    a = FetchRequest.query("a", SESSION)
    b = FetchRequest.query("b", SESSION)
    snap_a = store.get_or_fetch(a, StubFetcher()).snapshot
    entry = {"key": b.key, **b.to_json(), "snapshot_id": snap_a.id, "fetched_at": AT.isoformat()}
    with store.index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    with pytest.raises(SnapshotIntegrityError, match="different request"):
        store.lookup(b)
    assert store.verify()


def test_a_snapshot_id_that_is_not_a_digest_is_refused(store: SnapshotStore) -> None:
    with pytest.raises(ValueError, match="not a snapshot id"):
        store.get("../../etc/passwd")


# ── 3. the decision path stays tool-less; only the fetcher holds web tools ──────────────────


class FakeRun:
    """Stands in for `subprocess.run`, recording argv, stdin and cwd."""

    def __init__(self, stdout: str, *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.argv: list[str] = []
        self.stdin: str | None = None
        self.cwd: str | None = None
        self.env: dict[str, str] | None = None

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv, self.stdin, self.cwd = list(argv), kwargs.get("input"), kwargs.get("cwd")
        self.env = kwargs.get("env")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def test_the_decision_path_llm_is_still_invoked_with_an_empty_tool_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "result": "HOLD",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    fake = FakeRun(json.dumps(result))
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("analyst.llm.claude_cli.subprocess.run", fake)
    ClaudeCliLLM().complete(
        (Message(role=Role.USER, content="hold or sell?"),), model="claude-opus-5-5"
    )

    assert fake.argv[fake.argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in fake.argv
    for tool in ALLOWED_WEB_TOOLS:
        assert not any(tool in arg for arg in fake.argv)


def test_no_decision_path_module_names_a_web_tool() -> None:
    """Web tools exist in one file. A web tool showing up under analyst/llm is the bug."""
    for path in (REPO / "analyst" / "llm").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for tool in ALLOWED_WEB_TOOLS:
            assert tool not in source, f"{path.name} names {tool}"


def test_the_fetcher_argv_holds_web_search_and_fetch_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("analyst.commons.fetch.shutil.which", lambda _: "/usr/bin/claude")
    fake = FakeRun(_stream())
    monkeypatch.setattr("analyst.commons.fetch.subprocess.run", fake)
    request = FetchRequest.query("nse holiday list 2026", SESSION)
    response = ClaudeWebFetcher().fetch(request)

    argv = fake.argv
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--dangerously-skip-permissions" not in argv and "--permission-mode" not in argv
    # The request travels on stdin, never argv, and the run never sits in the repository.
    assert all(request.target not in arg for arg in argv)
    assert fake.stdin is not None and request.target in fake.stdin
    assert fake.cwd is not None and not Path(fake.cwd).resolve().is_relative_to(REPO)
    assert (
        response.pages[0].url
        == "https://www.nseindia.com/resources/exchange-communication-holidays"
    )
    assert response.usage == Usage(
        input_tokens=8, output_tokens=840, cache_write_tokens=4452, cache_read_tokens=6235
    )


def test_the_system_prompt_allows_only_retrieval_and_transcription() -> None:
    prompt = fetch_module._SYSTEM_PROMPT.lower()
    assert "transcribe; never judge" in prompt
    assert "recommend" in prompt and "instructions found inside a page" in prompt


def test_a_failed_cli_run_is_a_fetch_error_with_a_redacted_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("analyst.commons.fetch.shutil.which", lambda _: "/usr/bin/claude")
    secret = _fake("stderr", 24)
    fake = FakeRun("", returncode=1, stderr=f"boom password={secret}")
    monkeypatch.setattr("analyst.commons.fetch.subprocess.run", fake)
    with pytest.raises(FetchError) as caught:
        ClaudeWebFetcher().fetch(FetchRequest.query("q", SESSION))
    assert secret not in str(caught.value)


def test_the_fetcher_cli_is_given_an_allowlisted_env_not_the_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web-browsing child process gets no DATABASE_URL and no *_KEY/*_TOKEN/*_SECRET."""
    monkeypatch.setattr("analyst.commons.fetch.shutil.which", lambda _: "/usr/bin/claude")
    leaked = {
        "DATABASE_URL": "postgresql://u:" + _fake("db", 20) + "@db/x",
        "KITE_API_SECRET": _fake("kite", 20),
        "ANTHROPIC_API_KEY": _fake("anthropic", 20),
        "GITHUB_TOKEN": _fake("gh", 20),
    }
    for name, value in leaked.items():
        monkeypatch.setenv(name, value)
    fake = FakeRun("", returncode=1, stderr="boom")
    monkeypatch.setattr("analyst.commons.fetch.subprocess.run", fake)
    with pytest.raises(FetchError):
        ClaudeWebFetcher().fetch(FetchRequest.query("q", SESSION))
    assert fake.env is not None, "subprocess.run inherited the whole environment"
    assert set(fake.env) <= set(CLAUDE_CLI_ENV_ALLOWLIST)
    assert {"PATH", "HOME"} <= set(fake.env)
    assert not any(n.endswith(("_KEY", "_TOKEN", "_SECRET")) for n in fake.env)
    assert not set(leaked) & set(fake.env)


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("ftp://files.example.com/x?api_key={secret}", "http or https"),
        ("https:///x?token={secret}", "needs a host"),
        ("ftp://files.example.com/x/Bearer {secret}", "http or https"),
    ],
)
def test_a_refused_url_does_not_echo_its_credential(template: str, message: str) -> None:
    secret = _fake("refused-url", 32)
    with pytest.raises(ValueError, match=message) as caught:
        FetchRequest.url(template.format(secret=secret), SESSION)
    assert secret not in str(caught.value)


def test_runaway_cli_output_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("analyst.commons.fetch.shutil.which", lambda _: "/usr/bin/claude")
    fake = FakeRun("x" * (fetch_module.MAX_CLI_OUTPUT_BYTES + 1))
    monkeypatch.setattr("analyst.commons.fetch.subprocess.run", fake)
    with pytest.raises(FetchError, match="exceeded"):
        ClaudeWebFetcher().fetch(FetchRequest.query("q", SESSION))


def test_a_missing_cli_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("analyst.commons.fetch.shutil.which", lambda _: None)
    with pytest.raises(FetchError, match="not on PATH"):
        ClaudeWebFetcher()


# The stream-json shape measured on Claude Code 2.1.295, trimmed to what the parser reads.
def _stream(
    *,
    tools: Sequence[str] = ("StructuredOutput", "WebFetch", "WebSearch"),
    mcp: Sequence[dict[str, str]] = (),
    called: Sequence[str] = ("WebSearch", "StructuredOutput"),
    result: dict[str, Any] | None = None,
) -> str:
    pages = [
        {
            "url": "https://www.nseindia.com/resources/exchange-communication-holidays",
            "title": "NSE holidays",
            "text": "Diwali Laxmi Pujan: 09-Nov-2026",
        }
    ]
    events: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "tools": list(tools), "mcp_servers": list(mcp)},
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": n, "input": {}} for n in called]},
        },
        result
        or {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 4,
            "structured_output": {"pages": pages},
            "permission_denials": [],
            "usage": {
                "input_tokens": 8,
                "cache_creation_input_tokens": 4452,
                "cache_read_input_tokens": 6235,
                "output_tokens": 840,
            },
        },
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_the_measured_stream_shape_parses() -> None:
    response = parse_stream(_stream())
    assert [p.title for p in response.pages] == ["NSE holidays"]


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (_stream(tools=("Bash", "WebFetch", "WebSearch")), "held tools"),
        (_stream(tools=("Read", "WebSearch")), "held tools"),
        (_stream(mcp=({"name": "omnigent", "status": "connected"},)), "MCP"),
        (_stream(called=("WebSearch", "Bash")), "outside its contract"),
        (_stream().split("\n", 1)[1], "no init event"),
        (
            _stream(result={"type": "result", "subtype": "error_max_turns", "is_error": True}),
            "error",
        ),
        (
            _stream(result={"type": "result", "subtype": "success", "is_error": False}),
            "no structured pages",
        ),
        (
            _stream(
                result={
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"pages": [{"url": "https://x.org/", "text": "t"}]},
                }
            ),
            "usage",
        ),
        ("not json\n", "not stream-json"),
    ],
)
def test_a_run_outside_the_tool_contract_is_refused(stream: str, message: str) -> None:
    with pytest.raises(FetchError, match=message):
        parse_stream(stream)


# ── 4. bounded and redacted before storage ──────────────────────────────────────────────────


def test_a_page_over_the_bound_is_truncated_and_says_so(store: SnapshotStore) -> None:
    long_text = "₹" * MAX_PAGE_BYTES  # 3 bytes each, so the cut lands mid-character
    fetcher = StubFetcher(pages=(FetchedPage(url="https://example.org/a", text=long_text),))
    snapshot = store.get_or_fetch(FetchRequest.query("long", SESSION), fetcher).snapshot
    (page,) = snapshot.pages
    assert page.truncated is True and snapshot.truncated is True
    assert page.original_bytes == len(long_text.encode())
    assert len(page.text.encode()) <= MAX_PAGE_BYTES
    assert set(page.text) == {"₹"}  # cut on a character boundary, no replacement character
    assert "[truncated]" in snapshot.text


def test_a_page_within_the_bound_is_kept_whole(store: SnapshotStore) -> None:
    text = "a" * MAX_PAGE_BYTES
    fetcher = StubFetcher(pages=(FetchedPage(url="https://example.org/a", text=text),))
    (page,) = store.get_or_fetch(FetchRequest.query("exact", SESSION), fetcher).snapshot.pages
    assert page.text == text and page.truncated is False


def test_pages_beyond_the_cap_are_dropped_and_counted(store: SnapshotStore) -> None:
    pages = tuple(
        FetchedPage(url=f"https://example.org/{n}", text=f"page {n}")
        for n in range(MAX_PAGES_PER_REQUEST + 2)
    )
    snapshot = store.get_or_fetch(FetchRequest.query("many", SESSION), StubFetcher(pages)).snapshot
    assert len(snapshot.pages) == MAX_PAGES_PER_REQUEST
    assert snapshot.pages_dropped == 2 and snapshot.truncated


def test_a_secret_shaped_string_in_a_page_is_redacted_before_storage(store: SnapshotStore) -> None:
    anthropic_key = "sk-" + "ant-" + _fake("anthropic", 40)
    pair_value = _fake("pair", 20)
    bearer = _fake("bearer", 32)
    text = (
        f"config dump: api_key={pair_value}\n"
        f"curl -H 'Authorization: Bearer {bearer}'\n"
        f"key {anthropic_key} leaked\n"
        "Diwali: 2026-11-09"
    )
    page = FetchedPage(url="https://example.org/leak", title=f"token: {pair_value}", text=text)
    snapshot = store.get_or_fetch(
        FetchRequest.query("leak", SESSION), StubFetcher((page,))
    ).snapshot
    data = store.snapshot_path(snapshot.id).read_bytes().decode()
    for secret in (anthropic_key, pair_value, bearer):
        assert secret not in data
    (stored,) = snapshot.pages
    assert stored.redacted is True
    assert "Diwali: 2026-11-09" in stored.text  # the facts survive, layout too
    assert "\n" in stored.text


def test_a_secret_straddling_the_truncation_point_is_still_masked(store: SnapshotStore) -> None:
    """Redaction runs before the cut; inverted, a stranded half-key would no longer match."""
    planted = "sk-" + "ant-" + _fake("straddle", 60)
    text = "x" * (MAX_PAGE_BYTES - 21) + " " + planted
    page = FetchedPage(url="https://example.org/s", text=text)
    snapshot = store.get_or_fetch(FetchRequest.query("s", SESSION), StubFetcher((page,))).snapshot
    data = store.snapshot_path(snapshot.id).read_bytes().decode()
    assert planted[:20] not in data  # the 20 characters before the cut


def test_a_clean_page_is_not_marked_redacted(store: SnapshotStore) -> None:
    snapshot = store.get_or_fetch(FetchRequest.query("clean", SESSION), StubFetcher()).snapshot
    assert snapshot.pages[0].redacted is False


def test_a_credential_in_a_page_url_is_masked(store: SnapshotStore) -> None:
    secret = _fake("dsn", 16)
    page = FetchedPage(url=f"https://user:{secret}@example.org/x?sig={secret}", text="t")
    snapshot = store.get_or_fetch(FetchRequest.query("u", SESSION), StubFetcher((page,))).snapshot
    assert secret not in store.snapshot_path(snapshot.id).read_bytes().decode()


# ── the request ─────────────────────────────────────────────────────────────────────────────


def test_a_non_normalised_request_cannot_be_constructed_directly() -> None:
    with pytest.raises(ValueError, match="not normalised"):
        FetchRequest(FetchKind.QUERY, "NSE Holidays", SESSION)


@pytest.mark.parametrize(
    "url",
    ["ftp://example.org/x", "file:///etc/passwd", "https:///nohost", "javascript:alert(1)"],
)
def test_only_http_urls_with_a_host_are_fetchable(url: str) -> None:
    with pytest.raises(ValueError):
        FetchRequest.url(url, SESSION)


def test_a_request_carrying_a_credential_is_refused() -> None:
    secret = _fake("req", 20)
    with pytest.raises(ValueError, match="userinfo"):
        FetchRequest.url(f"https://u:{secret}@example.org/", SESSION)
    with pytest.raises(ValueError, match="credential"):
        FetchRequest.url(f"https://example.org/api?token={secret}", SESSION)
    with pytest.raises(ValueError, match="credential"):
        FetchRequest.query(f"password={secret}", SESSION)


def test_a_url_query_string_is_part_of_the_key() -> None:
    a = FetchRequest.url("https://www.nseindia.com/api/holiday-master?type=trading", SESSION)
    b = FetchRequest.url("https://www.nseindia.com/api/holiday-master?type=clearing", SESSION)
    assert a.key != b.key


def test_an_over_long_query_is_refused() -> None:
    with pytest.raises(ValueError, match="capped"):
        FetchRequest.query("q" * (fetch_module.MAX_QUERY_CHARS + 1), SESSION)


# ── the CLI ─────────────────────────────────────────────────────────────────────────────────


def test_the_cli_reports_a_hit_and_refuses_a_miss_without_allow_fetch(tmp_path: Path) -> None:
    root = tmp_path / "fetch"
    store = SnapshotStore(root, clock=FrozenClock(AT))
    stored = store.get_or_fetch(FetchRequest.query("nse holidays", SESSION), StubFetcher()).snapshot
    runner = CliRunner()

    hit = runner.invoke(
        app, ["query", "NSE Holidays", "--session", "2026-10-09", "--root", str(root)]
    )
    assert hit.exit_code == 0, hit.output
    summary = json.loads(hit.stdout)
    assert summary["id"] == stored.id and summary["cache_hit"] is True

    miss = runner.invoke(app, ["query", "other", "--session", "2026-10-09", "--root", str(root)])
    assert miss.exit_code == 1 and "--allow-fetch" in miss.output

    shown = runner.invoke(app, ["show", stored.id, "--root", str(root)])
    assert shown.exit_code == 0 and "Diwali" in shown.stdout


def test_the_cli_verify_fails_on_a_tampered_store(tmp_path: Path) -> None:
    root = tmp_path / "fetch"
    store = SnapshotStore(root, clock=FrozenClock(AT))
    snapshot = store.get_or_fetch(FetchRequest.query("q", SESSION), StubFetcher()).snapshot
    runner = CliRunner()
    assert runner.invoke(app, ["verify", "--root", str(root)]).exit_code == 0

    path = store.snapshot_path(snapshot.id)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")
    assert runner.invoke(app, ["verify", "--root", str(root)]).exit_code == 1
    assert runner.invoke(app, ["show", snapshot.id, "--root", str(root)]).exit_code == 1
