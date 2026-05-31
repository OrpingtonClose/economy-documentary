"""User-authored test case storage for the Component Playground.

The playground's "Save as case" flow writes cases to a JSON sidecar
at ``server/strands_agents/playground/user_cases/<component_id>.json``.
Keeping the user corpus in a separate tree means:

* The canonical CI experiments stay the single source of truth for
  regression testing — user exploration never perturbs the 85-case
  atlas counts the evaluators assert against.
* The on-disk shape is a plain JSON array, trivially reviewable as a
  PR diff. The ``preview_diff`` helper renders that diff
  deterministically so the frontend can show the user exactly what
  will land before they commit.
* Loading user cases is best-effort and isolated: a malformed file
  surfaces as an empty list and a logged warning, never a boot
  failure for the catalog endpoint.

Case replay / evaluation still flows through the existing
``Case`` → task adapter → evaluator stack path. ``UserCase.to_case``
materialises a ``strands_evals.case.Case`` so the run endpoint can
look user cases up by name identically to canonical ones.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from strands_evals.case import Case  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

#: On-disk root for user-authored cases. Override via ``base_dir`` on
#: every public helper so tests can point at a temp directory without
#: monkeypatching module globals.
DEFAULT_USER_CASES_DIR: Path = (
    Path(__file__).resolve().parent / "user_cases"
)

#: Allowed ``role`` values. Matches the atlas chip palette so the
#: frontend renders user cases with the same green/red/amber colouring
#: as canonical ones.
VALID_ROLES: frozenset[str] = frozenset({"pass", "neg", "edge"})

#: Case names share a namespace with canonical cases at run time
#: (``GET /components/{id}`` serialises both). Keeping the allowed
#: character set narrow avoids ambiguity on the URL layer — every case
#: name is a valid path segment and a valid filename if someone ever
#: exports it.
_CASE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


class UserCase(BaseModel):
    """One user-authored case.

    This model is both the wire format (POST body + GET payload) and
    the on-disk format (one entry in the JSON array). Keeping the
    shapes identical means the diff preview is a straight JSON
    round-trip — no "translate for storage" step to go wrong.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Unique case name within the component. Alphanumerics "
            "plus ``_``/``-``; 1-64 chars. Must not collide with a "
            "canonical case name."
        ),
    )
    role: str = Field(
        default="pass",
        description="One of ``pass`` / ``neg`` / ``edge`` for atlas chip colouring.",
    )
    input: Any = Field(
        ...,
        description="Input payload. Shape is component-dependent; the "
        "server validates it at replay time, not here.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional free-form metadata (notes, source prompt, etc.).",
    )
    notes: str | None = Field(
        default=None,
        description="Human-facing comment shown next to the case in the UI.",
    )
    created_at: str | None = Field(
        default=None,
        description="UTC ISO-8601 timestamp, stamped server-side on write.",
    )
    created_by: str | None = Field(
        default=None,
        description="Optional attribution — e.g. a CLI user or account id.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _CASE_NAME_PATTERN.match(value):
            raise ValueError(
                "name must match ``[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}``"
            )
        return value

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}; got {value!r}"
            )
        return value

    def to_case(self) -> Case[Any, Any]:
        """Materialise a ``strands_evals`` ``Case`` for replay.

        The session id is synthesised deterministically from the case
        name so repeated replays of the same user case thread through
        the same session — useful for trajectory-aware evaluators that
        key caches off the session id.
        """
        return Case[Any, Any](
            name=self.name,
            session_id=f"user-{self.name}",
            input=self.input,
            metadata=dict(self.metadata),
        )

    def stamped(self) -> "UserCase":
        """Return a copy with ``created_at`` set to the current UTC."""
        if self.created_at:
            return self
        return self.model_copy(
            update={"created_at": datetime.now(timezone.utc).isoformat()}
        )


def user_cases_path(component_id: str, base_dir: Path | None = None) -> Path:
    """Return the JSON file backing ``component_id``'s user cases.

    File is created lazily on first write. A missing file means "no
    user cases yet", not an error — ``load_user_cases`` tolerates it.
    """
    root = base_dir or DEFAULT_USER_CASES_DIR
    return root / f"{component_id}.json"


def load_user_cases(
    component_id: str, base_dir: Path | None = None
) -> list[UserCase]:
    """Load every user case for ``component_id``.

    Returns an empty list when the file is missing. Malformed entries
    (bad JSON, wrong shape, unknown role) are dropped with a warning
    rather than raised — the catalog must not 500 because someone
    hand-edited a sidecar.
    """
    path = user_cases_path(component_id, base_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "user_cases: ignoring malformed %s: %s", path, exc
        )
        return []
    if not isinstance(raw, list):
        logger.warning(
            "user_cases: expected list in %s, got %s", path, type(raw).__name__
        )
        return []
    out: list[UserCase] = []
    for index, entry in enumerate(raw):
        try:
            out.append(UserCase.model_validate(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "user_cases: dropping entry %d in %s: %s", index, path, exc
            )
    return out


def _serialise(cases: Iterable[UserCase]) -> str:
    """Canonicalise the on-disk JSON so diffs stay stable.

    ``sort_keys`` within each entry + a trailing newline is enough to
    keep the file a friendly thing to review as a PR.
    """
    payload = [c.model_dump(exclude_none=True) for c in cases]
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def preview_diff(
    component_id: str,
    new_case: UserCase,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a preview bundle for ``new_case`` without touching disk.

    The bundle carries everything the UI needs to render a commit
    dialog: the existing contents, the proposed contents, a unified
    diff, and the file path the user will see in the eventual PR.
    """
    existing = load_user_cases(component_id, base_dir)
    path = user_cases_path(component_id, base_dir)
    _reject_on_collision(new_case, existing)
    updated = [*existing, new_case.stamped()]
    before = _serialise(existing) if path.exists() else ""
    after = _serialise(updated)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(_repo_relative(path)),
            tofile=str(_repo_relative(path)),
            n=3,
        )
    )
    return {
        "file_path": str(_repo_relative(path)),
        "existed": path.exists(),
        "diff": diff,
        "before": before,
        "after": after,
        "case_count_before": len(existing),
        "case_count_after": len(updated),
    }


def append_user_case(
    component_id: str,
    new_case: UserCase,
    base_dir: Path | None = None,
) -> UserCase:
    """Append ``new_case`` to the component's sidecar file.

    The stamped case (with ``created_at`` filled in) is returned so
    the endpoint's response body matches what just landed on disk.
    """
    existing = load_user_cases(component_id, base_dir)
    _reject_on_collision(new_case, existing)
    stamped = new_case.stamped()
    path = user_cases_path(component_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    updated = [*existing, stamped]
    path.write_text(_serialise(updated), encoding="utf-8")
    return stamped


class DuplicateCaseNameError(ValueError):
    """Raised when a user case name collides with an existing case."""


def _reject_on_collision(
    new_case: UserCase, existing: Iterable[UserCase]
) -> None:
    for case in existing:
        if case.name == new_case.name:
            raise DuplicateCaseNameError(
                f"user case name already in use: {new_case.name!r}"
            )


def _repo_relative(path: Path) -> Path:
    """Return ``path`` relative to the repo root when possible.

    Used to give the frontend a short, human-readable file path for
    the commit dialog (``server/strands_agents/playground/user_cases/c01.json``)
    rather than a noisy absolute filesystem path.
    """
    try:
        repo_root = Path(__file__).resolve().parents[3]
        return path.resolve().relative_to(repo_root)
    except ValueError:
        return path
