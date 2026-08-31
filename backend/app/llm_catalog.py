"""Bundled llama.cpp GGUF catalog. Weights are not stored in git.

Host CLI (fetch-llm) and Core (Health UI) share this module. Stdlib only —
do not import SQLAlchemy or PyYAML here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Compose default when FORGESRE_LLM_GGUF is unset (N's screenshot).
DEFAULT_FILENAME = "model.gguf"
DEFAULT_CTX = 8192
DEFAULT_ID = "qwen2.5-14b"
GGUF_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.gguf$")

# Official Qwen/Qwen2.5-14B-Instruct-GGUF Q4_K_M is now a 3-way split
# (qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf …). The old single-file
# URL 404s. Bundled default stays one file named model.gguf.
# Qwen/Qwen3-1.7B-Instruct-GGUF does not exist (401). Official
# Qwen/Qwen3-1.7B-GGUF has Q8_0 only, no Q4_K_M. Light 1.7B pin is unsloth
# Qwen3-1.7B-Q4_K_M.gguf (HEAD 200). Qwen2-1.5B Instruct Q4_K_M exists
# (qwen2-1_5b-instruct-q4_k_m.gguf, ~986 MB) but Qwen2.5-1.5B-Instruct is
# the catalog pin.

CATALOG: list[dict[str, Any]] = [
    {
        "id": "qwen2.5-14b",
        "name": "Qwen2.5-14B-Instruct Q4_K_M",
        "filename": DEFAULT_FILENAME,
        "url": (
            "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF"
            "/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
        ),
        "ctx": 8192,
        "min_bytes": 4_000_000_000,
        "size_hint": "~8.4 GB",
        "ram_hint": "16 GB",
        "default": True,
        "kind": "full",
        "notes": (
            "Bundled default. Unset FORGESRE_LLM_GGUF still loads /models/model.gguf. "
            "Official Qwen Q4_K_M is sharded (3 files); this is the bartowski single-file GGUF."
        ),
    },
    {
        "id": "qwen3-1.7b",
        "name": "Qwen3-1.7B Q4_K_M",
        "filename": "Qwen3-1.7B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF"
            "/resolve/main/Qwen3-1.7B-Q4_K_M.gguf"
        ),
        "ctx": 4096,
        "min_bytes": 400_000_000,
        "size_hint": "~1.0 GB",
        "ram_hint": "8 GB",
        "default": False,
        "kind": "light",
        "notes": (
            "No Qwen3-1.7B-Instruct-GGUF on Hugging Face. Official Qwen/Qwen3-1.7B-GGUF "
            "ships Q8_0 only. This is unsloth Qwen3-1.7B-Q4_K_M.gguf."
        ),
    },
    {
        "id": "qwen2.5-1.5b",
        "name": "Qwen2.5-1.5B-Instruct Q4_K_M",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "url": (
            "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF"
            "/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"
        ),
        "ctx": 4096,
        "min_bytes": 400_000_000,
        "size_hint": "~1.1 GB",
        "ram_hint": "8 GB",
        "default": False,
        "kind": "light",
        "notes": (
            "Official Qwen Instruct GGUF. Prefer this over Qwen2-1.5B "
            "(qwen2-1_5b-instruct-q4_k_m.gguf)."
        ),
    },
    {
        "id": "qwen3-4b",
        "name": "Qwen3-4B Q4_K_M",
        "filename": "Qwen3-4B-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/Qwen/Qwen3-4B-GGUF"
            "/resolve/main/Qwen3-4B-Q4_K_M.gguf"
        ),
        "ctx": 4096,
        "min_bytes": 1_000_000_000,
        "size_hint": "~2.5 GB",
        "ram_hint": "8 GB",
        "default": False,
        "kind": "medium",
        "notes": "Official Qwen GGUF. Same file as the older lab wget path.",
    },
]

ALIASES: dict[str, str] = {
    "default": DEFAULT_ID,
    "full": DEFAULT_ID,
    "14b": DEFAULT_ID,
    "qwen2.5-14b-instruct": DEFAULT_ID,
    "1.7b": "qwen3-1.7b",
    "qwen3": "qwen3-1.7b",
    "qwen3-1.7b-instruct": "qwen3-1.7b",
    "1.5b": "qwen2.5-1.5b",
    "qwen2.5": "qwen2.5-1.5b",
    "4b": "qwen3-4b",
    "qwen3-4b-instruct": "qwen3-4b",
}


def safe_gguf_name(value: str | None, default: str = DEFAULT_FILENAME) -> str:
    name = Path(str(value or "").strip() or default).name
    if not GGUF_NAME_RE.fullmatch(name):
        return default
    return name


def default_entry() -> dict[str, Any]:
    for row in CATALOG:
        if row.get("default"):
            return dict(row)
    return dict(CATALOG[0])


def resolve(model_id: str | None) -> dict[str, Any]:
    raw = str(model_id or "").strip().lower()
    if not raw:
        return default_entry()
    raw = ALIASES.get(raw, raw)
    for row in CATALOG:
        if str(row["id"]).lower() == raw:
            return dict(row)
    ids = ", ".join(str(row["id"]) for row in CATALOG)
    raise KeyError(f"unknown LLM catalog id {model_id!r}. Known: {ids}")


def entry_by_filename(filename: str) -> dict[str, Any] | None:
    name = safe_gguf_name(filename)
    for row in CATALOG:
        if row["filename"] == name:
            return dict(row)
    return None


def read_dotenv_map(path: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if path is None or not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def dotenv_path_from_env() -> Path | None:
    raw = os.environ.get("FORGESRE_DOTENV") or ""
    if raw:
        return Path(raw)
    return None


def models_dir_from_env() -> Path:
    raw = os.environ.get("FORGESRE_MODELS_DIR") or ""
    if raw:
        return Path(raw)
    data = os.environ.get("FORGESRE_DATA") or "data"
    return Path(data) / "models"


def current_gguf(*, dotenv: dict[str, str] | None = None) -> str:
    env = dotenv or {}
    return safe_gguf_name(env.get("FORGESRE_LLM_GGUF") or os.environ.get("FORGESRE_LLM_GGUF"))


def current_ctx(*, dotenv: dict[str, str] | None = None) -> int:
    env = dotenv or {}
    raw = (env.get("FORGESRE_LLM_CTX") or os.environ.get("FORGESRE_LLM_CTX") or "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_CTX


def file_status(path: Path, min_bytes: int) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "bytes": 0, "ok": False}
    size = path.stat().st_size
    return {"present": True, "bytes": size, "ok": size >= int(min_bytes)}


def catalog_status(
    *,
    models_dir: Path | None = None,
    dotenv_path: Path | None = None,
) -> dict[str, Any]:
    env_map = read_dotenv_map(dotenv_path if dotenv_path is not None else dotenv_path_from_env())
    active = current_gguf(dotenv=env_map)
    ctx = current_ctx(dotenv=env_map)
    root = models_dir if models_dir is not None else models_dir_from_env()
    rows: list[dict[str, Any]] = []
    for item in CATALOG:
        path = root / str(item["filename"])
        disk = file_status(path, int(item["min_bytes"]))
        row = dict(item)
        row.update(
            {
                "path": str(path),
                "present": disk["present"],
                "bytes": disk["bytes"],
                "ok": disk["ok"],
                "active": active == item["filename"],
            }
        )
        rows.append(row)
    active_entry = entry_by_filename(active)
    return {
        "active_filename": active,
        "active_ctx": ctx,
        "active_id": (active_entry or {}).get("id") or "",
        "models_dir": str(root),
        "default_filename": DEFAULT_FILENAME,
        "default_ctx": DEFAULT_CTX,
        "models": rows,
    }


def format_size(n: int) -> str:
    if n <= 0:
        return "—"
    gb = n / (1024**3)
    if gb >= 1:
        return f"{gb:.1f}G"
    mb = n / (1024**2)
    return f"{mb:.0f}M"


def format_table(status: dict[str, Any]) -> str:
    lines = [
        "ForgeSRE llama.cpp GGUF catalog (weights are not in git).",
        f"Active file: {status['active_filename']}   ctx: {status['active_ctx']}",
        "Unset FORGESRE_LLM_GGUF still loads /models/model.gguf (14B default).",
        "",
        f"{'id':<14} {'kind':<7} {'name':<32} {'file':<38} {'ctx':<5} {'disk':<10} active",
    ]
    for row in status["models"]:
        disk = "no"
        if row["present"]:
            disk = format_size(int(row["bytes"]))
            if not row["ok"]:
                disk += "!"
        mark = "*" if row["active"] else ""
        lines.append(
            f"{row['id']:<14} {row['kind']:<7} {row['name']:<32} {row['filename']:<38} "
            f"{row['ctx']:<5} {disk:<10} {mark}"
        )
    lines.extend(
        [
            "",
            "Fetch / switch (never ./install.sh on a live box):",
            "  ./forgesre fetch-llm",
            "  ./forgesre fetch-llm --list",
            "  ./forgesre fetch-llm --model qwen3-1.7b",
            "  ./forgesre fetch-llm --model qwen2.5-1.5b",
            "  ./forgesre fetch-llm use qwen2.5-14b",
            "Guide: docs/llm.md",
        ]
    )
    return "\n".join(lines) + "\n"


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForgeSRE llama.cpp GGUF catalog")
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "json", "resolve", "default"),
    )
    parser.add_argument("model_id", nargs="?")
    parser.add_argument("--models-dir", default="")
    parser.add_argument("--dotenv", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    models_dir = Path(args.models_dir) if args.models_dir else None
    dotenv = Path(args.dotenv) if args.dotenv else None
    try:
        if args.action == "default":
            print(json.dumps(default_entry(), indent=2))
            return 0
        if args.action == "resolve":
            print(json.dumps(resolve(args.model_id)))
            return 0
        status = catalog_status(models_dir=models_dir, dotenv_path=dotenv)
        if args.action == "json" or args.as_json:
            print(json.dumps(status, indent=2))
            return 0
        sys.stdout.write(format_table(status))
        return 0
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
