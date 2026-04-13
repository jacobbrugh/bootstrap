"""Phase idempotency state.

Each phase can write a small JSON file to `$XDG_STATE_HOME/bootstrap/phases/`
recording its last run, result, and the host fingerprint at the time. The
orchestrator uses these records to short-circuit phases that have already
succeeded on the current host fingerprint.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from bootstrap.lib.paths import PHASE_STATE_DIR

PhaseResult = Literal["ok", "skipped", "failed"]


@dataclass(slots=True)
class PhaseState:
    """On-disk record of one phase execution."""

    name: str
    last_run: str
    result: PhaseResult
    host_fingerprint: str
    metadata: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> PhaseState:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"expected dict at top level, got {type(raw).__name__}")
        result = raw.get("result")
        if result not in ("ok", "skipped", "failed"):
            raise ValueError(f"invalid result: {result!r}")
        metadata_raw = raw.get("metadata", {})
        if not isinstance(metadata_raw, dict):
            raise ValueError(f"metadata must be a dict, got {type(metadata_raw).__name__}")
        return cls(
            name=str(raw["name"]),
            last_run=str(raw["last_run"]),
            result=result,
            host_fingerprint=str(raw["host_fingerprint"]),
            metadata={str(k): str(v) for k, v in metadata_raw.items()},
        )


def state_path(phase_name: str) -> Path:
    return PHASE_STATE_DIR / f"{phase_name}.json"


def read(phase_name: str) -> PhaseState | None:
    path = state_path(phase_name)
    if not path.exists():
        return None
    return PhaseState.from_json(path.read_text())


def write(
    phase_name: str,
    *,
    result: PhaseResult,
    host_fingerprint: str,
    metadata: dict[str, str] | None = None,
) -> None:
    PHASE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = PhaseState(
        name=phase_name,
        last_run=datetime.now(tz=UTC).isoformat(),
        result=result,
        host_fingerprint=host_fingerprint,
        metadata=metadata or {},
    )
    state_path(phase_name).write_text(state.to_json())
