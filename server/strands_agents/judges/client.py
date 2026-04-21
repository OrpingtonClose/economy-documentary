"""Judge client — abstract interface, self-hosted HTTP impl, and mock.

Every judge (safety, AV primary, tiebreaker, fallback) speaks the same
request/response shape.  That matters because :class:`JudgeEnsemble` in
PR-C routes the same :class:`JudgeRequest` to multiple clients in
parallel and needs to reason about their verdicts without per-backend
branching.

Design rules:

1. Clients are stateless — a :class:`JudgeRequest` carries *all* context
   (prompt, artifacts, rubric).  No hidden threadlocals.
2. Failures are explicit: :attr:`JudgeResponse.ok` is ``False`` with
   :attr:`JudgeResponse.error` populated rather than raising.  The
   ensemble needs to grade and compare judges even when one of them
   fails, so exceptions would force defensive ``try/except`` at every
   call site.
3. :class:`MockJudgeClient` is a first-class shippable artifact — every
   evaluator test runs against it by default so the unit suite stays
   hermetic (no network, no GPU).  Live-fleet integration tests flip to
   :class:`HttpJudgeClient` via a pytest fixture.

Wire format on the server side is whatever the judge worker script
decides to expose; the contract here is the minimum it MUST surface.
"""

from __future__ import annotations

import abc
import json
import logging
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeRequest:
    """Single prompt sent to a judge.

    The shape is a strict subset of the OpenAI chat-completions body so
    porting between backends (llama.cpp, vLLM, TGI, Claude-as-fallback)
    is mechanical.

    Attributes:
        prompt: The user-facing prompt.  Should already be formatted
            with the rubric, few-shot examples, and the artifact
            serialisation the judge expects.
        system: Optional system prompt.  Evaluators use this to pin the
            judge's persona ("you are a content-safety adjudicator")
            without leaking the rubric into the user turn.
        images: List of URLs or base64 data-URIs for image attachments
            (per-scene stills, concept boards).  Empty for pure-text
            judges.
        audio_url: Optional URL to an audio artifact (WAV/MP3/FLAC).
            Only the AV judges consume this; safety/fallback ignore it.
        video_url: Optional URL to a video artifact (MP4/WebM).  Only
            the AV judges consume this.
        temperature: Sampling temperature.  Defaults to ``0.0`` because
            judge responses should be as deterministic as possible —
            repeatability is worth more than creativity here.
        max_tokens: Upper bound on the response length.  Defaults to
            ``1024``, which is enough for a structured verdict
            (score + reasoning) without encouraging rambling.
    """

    prompt: str
    system: str = ""
    images: tuple[str, ...] = field(default_factory=tuple)
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 1024

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the OpenAI-compatible JSON body the worker expects.

        Returns:
            Dictionary ready to be ``json.dumps``'d and POSTed.
        """

        messages: list[dict[str, Any]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})

        # Text content always present; attachments appended as
        # OpenAI-style content parts.  A pure-text judge ignores the
        # non-text parts, so sending them is safe even to the safety
        # judge (which will drop them server-side).
        user_parts: list[dict[str, Any]] = [{"type": "text", "text": self.prompt}]
        for image_url in self.images:
            user_parts.append({"type": "image_url", "image_url": {"url": image_url}})
        if self.audio_url:
            user_parts.append({"type": "audio_url", "audio_url": {"url": self.audio_url}})
        if self.video_url:
            user_parts.append({"type": "video_url", "video_url": {"url": self.video_url}})
        messages.append({"role": "user", "content": user_parts})

        return {
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class JudgeResponse:
    """Structured response from a judge.

    The ensemble treats :attr:`ok` and :attr:`error` as the first-class
    signal: evaluators that call a judge directly should always branch
    on :attr:`ok` before reading :attr:`text`.

    Attributes:
        ok: Whether the judge produced a well-formed verdict.
        text: Raw string the judge returned.  Evaluators parse this into
            score+reasoning using a rubric-specific regex.
        model: Model identifier the server reported.  Used for trace
            logging and for the ensemble to decide whether it got what
            it asked for (catches misconfigured routes).
        latency_ms: Wall-clock latency, including network.  Informational.
        usage: Token accounting from the server (``prompt_tokens`` /
            ``completion_tokens`` / ``total_tokens``).  Empty dict if
            the backend doesn't report it.
        error: Machine-readable error code when :attr:`ok` is ``False``.
            Callers MUST check :attr:`ok`; :attr:`error` alone is
            advisory.
    """

    ok: bool
    text: str = ""
    model: str = ""
    latency_ms: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dict (no tuples, no frozen containers)."""

        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract client
# ---------------------------------------------------------------------------


class JudgeClient(abc.ABC):
    """Protocol every judge implementation obeys.

    Subclasses MUST be safe to share across threads — the ensemble
    dispatches requests concurrently.  The stock implementations below
    use stateless urllib / dict lookup so thread safety is trivially
    satisfied.
    """

    #: Role this client fills (safety / av_primary / av_tiebreaker /
    #: fallback).  Used only for logging / tracing.
    role: str = "generic"

    @abc.abstractmethod
    def complete(self, request: JudgeRequest) -> JudgeResponse:
        """Submit one request and return the parsed response.

        Args:
            request: The prompt + attachments to send.

        Returns:
            :class:`JudgeResponse` with ``ok=True`` on success.  On any
            failure — network error, HTTP non-2xx, malformed JSON —
            returns ``ok=False`` with :attr:`JudgeResponse.error`
            populated; does NOT raise.
        """

    def close(self) -> None:
        """Release any connection resources.

        Default is no-op because the shipped clients are stateless.
        """


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_S = 120.0


class HttpJudgeClient(JudgeClient):
    """Client against a self-hosted FastAPI judge server.

    The server contract (implemented separately in
    ``scripts/judge_worker.py``) is:

    ``POST {base_url}/v1/chat/completions`` with an OpenAI-compatible
    JSON body and ``Bearer`` auth.  The judge worker is responsible for
    loading the underlying open-weight model (Gemma 4, Qwen3.5-Omni,
    SALMONN) and returning a standard OpenAI chat response.

    We use ``urllib.request`` instead of ``httpx`` to avoid adding a
    runtime dependency to the ``[strands]`` extra — the judge client is
    imported by unit tests that run in a minimal wheelhouse.

    Attributes:
        base_url: Root URL of the judge server (no trailing slash).
        api_key: Bearer token for the judge server.  The server runs on
            our Vast.ai fleet so the token is shared-secret rather than
            per-tenant.
        model: Model identifier to request.  Must match what the server
            loaded at boot.
        timeout_s: Request timeout.  Defaults to two minutes, which is
            generous for text-only prompts and tight for long videos;
            evaluators that exercise video pass a higher value.
        request_fn: Injectable transport for tests.  Defaults to a
            wrapper around :func:`urllib.request.urlopen`.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        role: str = "generic",
        request_fn: Optional[Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, bytes]]] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be non-empty")
        if not model:
            raise ValueError("model must be non-empty")
        # api_key may be empty in dev deployments of the judge worker
        # that disable auth; we don't reject it here.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self.role = role
        self._request_fn = request_fn or _urllib_post

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        """Send ``request`` to the judge server and return its verdict."""

        url = f"{self._base_url}/v1/chat/completions"
        body = request.to_payload()
        body["model"] = self._model
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            status, raw = self._request_fn(url, body, headers, self._timeout_s)
        except Exception as exc:
            logger.warning(
                "role=<%s>, url=<%s> | judge HTTP transport failed: %s",
                self.role,
                url,
                exc,
            )
            return JudgeResponse(ok=False, error=f"transport: {exc}")

        if status < 200 or status >= 300:
            snippet = raw[:200].decode("utf-8", errors="replace")
            logger.warning(
                "role=<%s>, status=<%d> | judge HTTP non-2xx: %s",
                self.role,
                status,
                snippet,
            )
            return JudgeResponse(ok=False, error=f"http_{status}: {snippet}")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return JudgeResponse(ok=False, error=f"json_decode: {exc}")

        return _parse_openai_response(parsed)


def _urllib_post(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> tuple[int, bytes]:
    """Thin urllib wrapper used as the default :attr:`HttpJudgeClient._request_fn`.

    Split into its own function so tests can monkeypatch the transport
    without touching the production network path.
    """

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError carries the body, which the caller uses for error
        # diagnostics.  Read it out so the status+snippet branch above
        # sees it.
        return exc.code, exc.read()


def _parse_openai_response(parsed: dict[str, Any]) -> JudgeResponse:
    """Pull the OpenAI-shaped fields out of a parsed JSON body.

    Tolerates missing ``usage`` and missing ``model`` fields because
    some worker builds omit them.  Returns ``ok=False`` only when the
    response is so malformed it has no choices at all — a degenerate
    but valid response (e.g. empty completion) is still ``ok=True``.
    """

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return JudgeResponse(ok=False, error="no_choices", model=str(parsed.get("model", "")))

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Multimodal servers return a list of content parts; concatenate
        # the text parts for the caller.
        text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    else:
        text = str(content or "")

    usage_raw = parsed.get("usage") or {}
    usage = {
        k: int(v)
        for k, v in usage_raw.items()
        if isinstance(v, (int, float)) and k in {"prompt_tokens", "completion_tokens", "total_tokens"}
    }
    return JudgeResponse(
        ok=True,
        text=text,
        model=str(parsed.get("model", "")),
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Mock client (hermetic, for unit tests)
# ---------------------------------------------------------------------------


class MockJudgeClient(JudgeClient):
    """In-memory judge used by unit tests.

    Two modes:

    1. **Canned responses** — pass a dict keyed by the prompt (or a
       prefix of it) to :func:`__init__`.  Useful when the test has a
       small number of deterministic prompts.
    2. **Callable** — pass a function that receives the request and
       returns either a string (becomes :attr:`JudgeResponse.text`) or
       a full :class:`JudgeResponse`.  Useful when the test wants to
       assert on request shape.

    If neither is provided, :meth:`complete` returns a stub
    ``{"score": 1.0, "reasoning": "stub"}`` JSON object so evaluators
    that forget to configure a mock still get a parseable response
    instead of crashing.
    """

    def __init__(
        self,
        responses: Optional[dict[str, str]] = None,
        *,
        callable: Optional[Callable[[JudgeRequest], str | JudgeResponse]] = None,
        role: str = "mock",
        model: str = "mock-judge",
    ) -> None:
        self._responses = responses or {}
        self._callable = callable
        self.role = role
        self._model = model
        self._calls: list[JudgeRequest] = []

    @property
    def calls(self) -> list[JudgeRequest]:
        """Requests observed so far.  Exposed for test assertions."""

        return list(self._calls)

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        self._calls.append(request)

        if self._callable is not None:
            result = self._callable(request)
            if isinstance(result, JudgeResponse):
                return result
            return JudgeResponse(ok=True, text=str(result), model=self._model)

        # Canned lookup: exact match first, then longest-prefix match.
        # The prefix fallback lets tests register a single canned
        # response keyed by the rubric header and have it apply to
        # every variation the evaluator generates.
        if request.prompt in self._responses:
            return JudgeResponse(ok=True, text=self._responses[request.prompt], model=self._model)
        prefix = _longest_matching_prefix(request.prompt, self._responses)
        if prefix is not None:
            return JudgeResponse(ok=True, text=self._responses[prefix], model=self._model)

        return JudgeResponse(
            ok=True,
            text='{"score": 1.0, "reasoning": "stub"}',
            model=self._model,
        )


def _longest_matching_prefix(prompt: str, table: dict[str, str]) -> Optional[str]:
    """Return the longest key in ``table`` that is a prefix of ``prompt``.

    Returns None if no key is a prefix.
    """

    match: Optional[str] = None
    for key in table:
        if prompt.startswith(key) and (match is None or len(key) > len(match)):
            match = key
    return match


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_judge_client(
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    role: str = "generic",
    mock_responses: Optional[dict[str, str]] = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> JudgeClient:
    """Construct the appropriate client for the runtime environment.

    If ``base_url`` is empty OR ``mock_responses`` is provided, returns
    a :class:`MockJudgeClient`.  Otherwise returns an
    :class:`HttpJudgeClient`.

    This is the single path evaluators take to get a judge handle — it
    makes "is this a real fleet or a pytest fixture" a one-line
    decision rather than a scattered ``if TEST:`` check.

    Args:
        base_url: URL of the self-hosted judge server.
        api_key: Bearer token for the server.
        model: Model identifier to request.
        role: Ensemble role.
        mock_responses: If non-None, forces the mock client and uses
            this dict as the canned table.
        timeout_s: HTTP timeout.

    Returns:
        A live :class:`JudgeClient` ready for ``.complete()``.
    """

    if mock_responses is not None or not base_url:
        return MockJudgeClient(mock_responses, role=role, model=model or "mock-judge")
    return HttpJudgeClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        role=role,
        timeout_s=timeout_s,
    )
