#!/usr/bin/env python3
"""Disabled direct bridge entrypoint; builds require the isolated runner gate."""

from __future__ import annotations

def main(argv: list[str] | None = None) -> int:
    del argv  # The standalone route has no runner-issued archive provenance token.
    raise RuntimeError(
        "BLOCKED: direct bridge invocation is disabled; use only a runner-authorized staged build."
    )


if __name__ == "__main__":
    raise SystemExit(main())
