"""Version information for the server and the wire protocol.

`SERVER_VERSION` is the human-facing release number. `GIT_COMMIT` is filled in
at install time (the installer writes ``app/_build_info.py``) or resolved from
the working tree when running from a checkout.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

SERVER_VERSION = "0.1.0"

#: Bumped whenever the controller protocol changes incompatibly.
#: Must stay in sync with ``firmware/include/protocol_generated.h``.
PROTOCOL_VERSION = 1


@lru_cache(maxsize=1)
def git_commit() -> str:
    """Best-effort git commit of the running code. Never raises."""
    try:
        from app import _build_info  # type: ignore[attr-defined]

        commit = getattr(_build_info, "GIT_COMMIT", "")
        if commit:
            return str(commit)
    except Exception:  # pragma: no cover - _build_info is optional
        pass

    repo_root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # pragma: no cover - git may be absent
        pass
    return "unknown"


def version_info() -> dict[str, str | int]:
    return {
        "server_version": SERVER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "git_commit": git_commit(),
    }
