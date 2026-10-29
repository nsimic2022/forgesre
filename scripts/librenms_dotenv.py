#!/usr/bin/env python3
"""Quote and repair LibreNMS APP_KEY lines in dotenv files.

Docker Compose env_file and Laravel phpdotenv both reject orphan lines
(not KEY=value / comment / blank). openssl base64 APP_KEY values contain
``/`` ``+`` ``=``; an unquoted write, or artisan stdout captured into
``data/librenms/data/.env``, makes Compose / ``lnms`` fail with
"failed to parse dotenv file" / "environment file is invalid".

Never rotates a valid ``base64:`` key. Does not write generic DB_PASSWORD
or REDIS_PASSWORD into the shared secrets file (NetBox uses those names).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import sys
from pathlib import Path

ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
APP_KEY_RE = re.compile(r"^base64:[A-Za-z0-9+/]{32,}={0,3}$")
B64_FRAG_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9]{8,}$")
QUOTE_CHARS = set(" \t#\"'`$\\\n\r/")


def random_hex(nbytes: int) -> str:
    return secrets.token_hex(nbytes)


def random_app_key() -> str:
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def random_node_id() -> str:
    return secrets.token_hex(8)


def is_valid_app_key(value: str) -> bool:
    return bool(APP_KEY_RE.fullmatch((value or "").strip()))


def unquote(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return text


def needs_quote(value: str) -> bool:
    if value.startswith("base64:"):
        return True
    return any(ch in QUOTE_CHARS for ch in value)


def format_assignment(key: str, value: str, *, quote: bool | None = None) -> str:
    if quote is None:
        quote = needs_quote(value)
    if quote:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def classify(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return "keep"
    if ASSIGN_RE.match(stripped):
        return "assign"
    return "orphan"


def parse_assignment(line: str) -> tuple[str, str] | None:
    match = ASSIGN_RE.match(line.strip())
    if not match:
        return None
    return match.group(1), unquote(match.group(2))


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    elif not text:
        text = ""
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _join_app_key_fragments(lines: list[str], start: int, partial: str) -> tuple[str, int]:
    """Append following base64-only orphan lines onto a truncated APP_KEY."""
    joined = partial
    consumed = 0
    if is_valid_app_key(joined):
        return joined, 0
    for line in lines[start:]:
        stripped = line.strip()
        if classify(line) != "orphan" or not B64_FRAG_RE.fullmatch(stripped):
            break
        joined += stripped
        consumed += 1
        if is_valid_app_key(joined):
            return joined, consumed
    return partial, 0


def repair_lines(lines: list[str]) -> list[str]:
    """Drop orphan lines; reassemble split base64 APP_KEY values."""
    out: list[str] = []
    skip = 0
    for idx, line in enumerate(lines):
        if skip:
            skip -= 1
            continue
        kind = classify(line)
        if kind == "keep":
            out.append(line)
            continue
        if kind == "orphan":
            continue
        parsed = parse_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        quote = key in {"APP_KEY", "LIBRENMS_APP_KEY"} or needs_quote(value)
        if key in {"APP_KEY", "LIBRENMS_APP_KEY"} and value.startswith("base64:"):
            joined, consumed = _join_app_key_fragments(lines, idx + 1, value)
            value = joined
            skip = consumed
        out.append(format_assignment(key, value, quote=quote))
    return out


def get_value(path: Path, key: str) -> str:
    found = ""
    for line in repair_lines(read_lines(path)):
        parsed = parse_assignment(line)
        if parsed and parsed[0] == key:
            found = parsed[1]
    return found


def upsert(path: Path, key: str, value: str, *, quote: bool | None = None, fill_only: bool = True) -> str:
    """Set KEY=value. fill_only keeps a non-empty existing value. Returns effective value."""
    lines = repair_lines(read_lines(path))
    existing = ""
    found_at: int | None = None
    for idx, line in enumerate(lines):
        parsed = parse_assignment(line)
        if parsed and parsed[0] == key:
            existing = parsed[1]
            found_at = idx
    effective = existing if (fill_only and existing) else value
    if fill_only and key in {"APP_KEY", "LIBRENMS_APP_KEY"} and not is_valid_app_key(existing):
        effective = value
    if not effective and fill_only:
        effective = value
    assignment = format_assignment(key, effective, quote=quote)
    if found_at is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(assignment)
    else:
        lines[found_at] = assignment
    write_lines(path, lines)
    return effective


def seed_laravel_env(path: Path, app_key: str) -> None:
    """Write /data/.env with a valid APP_KEY so the image skips artisan capture."""
    lines = read_lines(path)
    current_key = ""
    current_node = ""
    for line in lines:
        parsed = parse_assignment(line)
        if not parsed:
            continue
        name, value = parsed
        if name == "APP_KEY" and is_valid_app_key(value):
            current_key = value
        elif name == "NODE_ID" and NODE_ID_RE.fullmatch(value):
            current_node = value
    key = current_key if is_valid_app_key(current_key) else app_key
    node = current_node or random_node_id()
    write_lines(
        path,
        [
            format_assignment("APP_KEY", key, quote=True),
            format_assignment("NODE_ID", node, quote=False),
        ],
    )


def ensure(
    *,
    secrets_path: Path,
    dotenv_path: Path | None,
    laravel_path: Path | None,
    db_password: str,
    db_root_password: str,
    redis_password: str,
    app_key: str,
    admin_password: str,
) -> str:
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    if not secrets_path.is_file():
        secrets_path.write_text("", encoding="utf-8")

    write_lines(secrets_path, repair_lines(read_lines(secrets_path)))

    db = upsert(secrets_path, "LIBRENMS_DB_PASSWORD", db_password)
    root_pw = upsert(secrets_path, "LIBRENMS_DB_ROOT_PASSWORD", db_root_password)
    redis_pw = upsert(secrets_path, "LIBRENMS_REDIS_PASSWORD", redis_password)
    key = upsert(secrets_path, "LIBRENMS_APP_KEY", app_key, quote=True)
    if not is_valid_app_key(key):
        key = upsert(secrets_path, "LIBRENMS_APP_KEY", app_key, quote=True, fill_only=False)
    upsert(secrets_path, "LIBRENMS_ADMIN_PASSWORD", admin_password)
    # LibreNMS image reads APP_KEY; MariaDB reads MYSQL_* / MARIADB_*.
    # Do not write generic DB_PASSWORD / REDIS_PASSWORD (NetBox collision).
    upsert(secrets_path, "APP_KEY", key, quote=True, fill_only=False)
    upsert(secrets_path, "MYSQL_PASSWORD", db, fill_only=True)
    upsert(secrets_path, "MYSQL_ROOT_PASSWORD", root_pw, fill_only=True)
    upsert(secrets_path, "MARIADB_PASSWORD", db, fill_only=True)
    upsert(secrets_path, "MARIADB_ROOT_PASSWORD", root_pw, fill_only=True)

    if dotenv_path is not None and dotenv_path.is_file():
        write_lines(dotenv_path, repair_lines(read_lines(dotenv_path)))
        upsert(dotenv_path, "LIBRENMS_URL", "http://127.0.0.1:8000")
        upsert(dotenv_path, "LIBRENMS_DB_PASSWORD", db)
        upsert(dotenv_path, "LIBRENMS_DB_ROOT_PASSWORD", root_pw)
        upsert(dotenv_path, "LIBRENMS_REDIS_PASSWORD", redis_pw)
        upsert(dotenv_path, "LIBRENMS_APP_KEY", key, quote=True, fill_only=False)

    if laravel_path is not None:
        seed_laravel_env(laravel_path, key)

    return key


def _paths_from_env() -> tuple[Path, Path, Path]:
    root = Path(os.environ.get("FORGESRE_ROOT") or Path(__file__).resolve().parents[1])
    data = Path(os.environ.get("FORGESRE_DATA") or (root / "data"))
    if not data.is_absolute():
        data = root / data
    return (
        root / "secrets" / "secrets.env",
        root / ".env",
        data / "librenms" / "data" / ".env",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", type=Path, default=None)
    parser.add_argument("--dotenv", type=Path, default=None)
    parser.add_argument("--laravel", type=Path, default=None)
    args = parser.parse_args(argv)
    secrets_path, dotenv_path, laravel_path = _paths_from_env()
    if args.secrets is not None:
        secrets_path = args.secrets
    if args.dotenv is not None:
        dotenv_path = args.dotenv
    if args.laravel is not None:
        laravel_path = args.laravel
    dotenv_arg = dotenv_path if dotenv_path.is_file() else None
    ensure(
        secrets_path=secrets_path,
        dotenv_path=dotenv_arg,
        laravel_path=laravel_path,
        db_password=random_hex(16),
        db_root_password=random_hex(16),
        redis_password=random_hex(16),
        app_key=random_app_key(),
        admin_password=random_hex(12),
    )
    print("LibreNMS secrets present (empty keys filled, existing values kept).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
