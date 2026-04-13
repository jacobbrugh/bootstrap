"""Phase module contract.

Every phase module under `bootstrap.phases.*` exports:

- `NAME: str` — short identifier used in logs and state files
- `run(ctx: Context) -> None` — do the work; raise `BootstrapError` on failure

The orchestrator imports phase modules and calls `module.run(ctx)`. Phases
never read env vars directly — they receive a populated `Context`.
"""

from __future__ import annotations
