#!/usr/bin/env python3
"""Generate the firmware's protocol header from the server's definition.

``app/turret/protocol.py`` is the single source of truth. This script emits
``firmware/include/protocol_generated.h`` so message names, error codes,
controller states and the protocol version cannot drift between the two
implementations.

Usage::

    python server/tools/gen_protocol_header.py            # write the header
    python server/tools/gen_protocol_header.py --check    # verify it is current

The ``--check`` mode is what CI (and `make check`) runs: it fails if the header
is stale, which is the only reliable way to keep a generated file honest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from app.turret.protocol import (  # noqa: E402
    CLOSE_BAD_REQUEST,
    CLOSE_REPLACED,
    CLOSE_UNAUTHORIZED,
    CLOSE_VERSION_MISMATCH,
    CONTROLLER_EVENTS,
    CONTROLLER_MESSAGE_TYPES,
    CONTROLLER_STATES,
    ERROR_CODES,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    SERVER_MESSAGE_TYPES,
)

HEADER_PATH = REPO_ROOT / "firmware" / "include" / "protocol_generated.h"


def _identifier(prefix: str, value: str) -> str:
    return f"{prefix}_{value.upper().replace('-', '_')}"


def _defines(prefix: str, values: tuple[str, ...]) -> str:
    width = max((len(_identifier(prefix, v)) for v in values), default=0)
    return "\n".join(f'#define {_identifier(prefix, value):<{width}} "{value}"' for value in values)


def render() -> str:
    return f"""/*
 * GENERATED FILE - DO NOT EDIT.
 *
 * Produced by server/tools/gen_protocol_header.py from
 * server/app/turret/protocol.py. Re-run the generator after changing the
 * protocol; `--check` fails the build if this file is stale.
 *
 * Prose specification: docs/PROTOCOL.md
 */

#pragma once

/* Wire protocol version. The server refuses to command a controller whose
 * version differs from its own. */
#define TURRET_PROTOCOL_VERSION {PROTOCOL_VERSION}

/* Largest accepted WebSocket frame, bytes. */
#define TURRET_MAX_FRAME_BYTES {MAX_FRAME_BYTES}

/* ---- server -> controller message types ---- */
{_defines("MSG", SERVER_MESSAGE_TYPES)}

/* ---- controller -> server message types ---- */
{_defines("MSG", CONTROLLER_MESSAGE_TYPES)}

/* ---- command rejection codes ---- */
{_defines("ERR", ERROR_CODES)}

/* ---- controller states (reported in `status.state`) ---- */
{_defines("STATE", CONTROLLER_STATES)}

/* ---- asynchronous controller events ---- */
{_defines("EVT", CONTROLLER_EVENTS)}

/* ---- WebSocket close codes used by the server ---- */
#define TURRET_CLOSE_BAD_REQUEST      {CLOSE_BAD_REQUEST}
#define TURRET_CLOSE_UNAUTHORIZED     {CLOSE_UNAUTHORIZED}
#define TURRET_CLOSE_REPLACED         {CLOSE_REPLACED}
#define TURRET_CLOSE_VERSION_MISMATCH {CLOSE_VERSION_MISMATCH}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the header is out of date")
    parser.add_argument("--output", type=Path, default=HEADER_PATH)
    args = parser.parse_args()

    content = render()
    if args.check:
        if not args.output.exists():
            print(f"missing generated header: {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            print(
                f"{args.output} is out of date - run: python {Path(__file__).name}",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output.name} is up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Explicit LF: the header is committed and read on Linux, so it must not
    # depend on which platform ran the generator.
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
