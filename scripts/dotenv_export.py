#!/usr/bin/env python3
"""Print bash-safe ``export KEY='value'`` lines from a dotenv file.

Never executes the file (no ``source``). Last assignment wins. Lines that are
not ``KEY=value`` (comments, blanks, a wrapped LibreNMS token) are skipped so
a ``2|secret`` value cannot become ``command not found``.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import librenms_dotenv as ld  # noqa: E402


def assignments(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in ld.read_lines(path):
        parsed = ld.parse_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        found[key] = value
    return found


def export_bash(path: Path) -> str:
    lines = [f"export {key}={shlex.quote(value)}" for key, value in assignments(path).items()]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="dotenv file (secrets/secrets.env, .env, …)")
    parser.add_argument(
        "--get",
        metavar="KEY",
        help="Print one unquoted value to stdout and exit (empty if missing)",
    )
    args = parser.parse_args(argv)
    if args.get:
        sys.stdout.write(assignments(args.path).get(args.get, ""))
        return 0
    sys.stdout.write(export_bash(args.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
