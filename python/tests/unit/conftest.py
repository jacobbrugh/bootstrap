from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap.lib import sh


@dataclass
class Call:
    argv: tuple[str, ...]
    env: dict[str, str] | None
    cwd: Path | None
    input_text: str | None


@dataclass
class ShRecorder:
    """A `sh.run` stand-in that records every call and answers by argv prefix.

    `answers` maps an argv prefix (tuple) to the stdout the command returns;
    the longest matching prefix wins; an unmatched command returns "".
    """

    calls: list[Call] = field(default_factory=list)
    answers: dict[tuple[str, ...], str] = field(default_factory=dict)

    def stdout_for(self, argv: tuple[str, ...]) -> str:
        best: tuple[str, ...] | None = None
        for prefix in self.answers:
            if argv[: len(prefix)] == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return self.answers[best] if best is not None else ""

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]

    def with_prefix(self, *prefix: str) -> list[Call]:
        return [c for c in self.calls if c.argv[: len(prefix)] == prefix]


@pytest.fixture
def sh_recorder(monkeypatch: pytest.MonkeyPatch) -> ShRecorder:
    rec = ShRecorder()

    async def fake_run(
        cmd: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        dry_run: bool = False,
        destructive: bool = True,
    ) -> object:
        argv = tuple(cmd)
        rec.calls.append(
            Call(
                argv=argv,
                env=dict(env) if env is not None else None,
                cwd=cwd,
                input_text=input_text,
            )
        )
        return SimpleNamespace(
            cmd=argv,
            returncode=0,
            stdout=rec.stdout_for(argv),
            stderr="",
            duration_s=0.0,
            dry_run_skipped=False,
            ok=lambda: True,
        )

    monkeypatch.setattr(sh, "run", fake_run)
    return rec


AnswerSetter = Callable[[tuple[str, ...], str], None]
