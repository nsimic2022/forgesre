"""Per-asset SNMP version and auth (assets.extras ``snmp_*`` keys) and the snmp_exporter auth name.

v2c + Default community is the config community (``public_v2`` in snmp.yml). v1 + Default uses
``public_v1`` (same community, version 1). A Custom community or any v3 USM user gets its own
``forgesre_<asset_id>`` auth, written into data/generated/snmp.yml by ``./forgesre snmp-auths``
(also run by ``./forgesre update`` / ``render-monitoring``). Until that auth exists in the rendered
snmp.yml, SNMP HTTP SD keeps sending ``public_v2`` — it never points snmp_exporter at an auth the
exporter does not have.

Community strings and v3 passwords never leave Core except through the bearer-protected
``/api/v1/sd/snmp-auths`` endpoint the render script reads. Forms, asset detail, the asset API,
journal and audit only see whether a secret is saved.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_AUTH = "public_v2"
DEFAULT_V1_AUTH = "public_v1"
ASSET_AUTH_PREFIX = "forgesre_"

SNMP_VERSIONS: tuple[tuple[str, str], ...] = (("v1", "v1"), ("v2c", "v2c"), ("v3", "v3 (USM)"))
VERSION_KEYS = tuple(key for key, _label in SNMP_VERSIONS)
DEFAULT_VERSION = "v2c"
COMMUNITY_MODES = ("default", "custom")
V3_LEVELS: tuple[tuple[str, str], ...] = (
    ("noAuthNoPriv", "noAuthNoPriv — username only"),
    ("authNoPriv", "authNoPriv — auth, no encryption"),
    ("authPriv", "authPriv — auth + encryption"),
)
V3_LEVEL_KEYS = tuple(key for key, _label in V3_LEVELS)
AUTH_PROTOCOLS = ("MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512")
PRIV_PROTOCOLS = ("DES", "AES", "AES192", "AES256", "AES192C", "AES256C")
USM_MIN_PASSWORD = 8

SECRET_KEYS = ("snmp_community", "snmp_v3_auth_pass", "snmp_v3_priv_pass")
PLAIN_KEYS = (
    "snmp_version",
    "snmp_community_mode",
    "snmp_v3_user",
    "snmp_v3_level",
    "snmp_v3_auth_proto",
    "snmp_v3_priv_proto",
)
SNMP_KEYS = PLAIN_KEYS + SECRET_KEYS
_LIMITS = {"snmp_community": 128, "snmp_v3_user": 64, "snmp_v3_auth_pass": 128, "snmp_v3_priv_pass": 128}


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    return "".join(ch for ch in str(value).strip() if ch not in "\r\n")[: _LIMITS.get(key, 64)]


def _choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    for choice in choices:
        if text.lower() == choice.lower():
            return choice
    return default


def normalize_snmp(raw: Any) -> dict[str, str]:
    """Stored SNMP keys: only what the chosen version uses. Nothing at all = v2c + Default community."""
    if not isinstance(raw, dict) or not any(raw.get(key) for key in SNMP_KEYS):
        return {}
    version = _choice(raw.get("snmp_version"), VERSION_KEYS, DEFAULT_VERSION)
    out: dict[str, str] = {"snmp_version": version}
    if version in {"v1", "v2c"}:
        community = _text(raw, "snmp_community")
        if _choice(raw.get("snmp_community_mode"), COMMUNITY_MODES, "default") == "custom" and community:
            out["snmp_community_mode"] = "custom"
            out["snmp_community"] = community
        else:
            out["snmp_community_mode"] = "default"
        return out
    user = _text(raw, "snmp_v3_user")
    if user:
        out["snmp_v3_user"] = user
    level = _choice(raw.get("snmp_v3_level"), V3_LEVEL_KEYS, "noAuthNoPriv")
    auth_pass = _text(raw, "snmp_v3_auth_pass")
    priv_pass = _text(raw, "snmp_v3_priv_pass")
    if level in {"authNoPriv", "authPriv"} and not auth_pass:
        level = "noAuthNoPriv"
    if level == "authPriv" and not priv_pass:
        level = "authNoPriv"
    out["snmp_v3_level"] = level
    if level != "noAuthNoPriv":
        out["snmp_v3_auth_proto"] = _choice(raw.get("snmp_v3_auth_proto"), AUTH_PROTOCOLS, "SHA")
        out["snmp_v3_auth_pass"] = auth_pass
    if level == "authPriv":
        out["snmp_v3_priv_proto"] = _choice(raw.get("snmp_v3_priv_proto"), PRIV_PROTOCOLS, "AES")
        out["snmp_v3_priv_pass"] = priv_pass
    return out


def snmp_settings(extras: Any) -> dict[str, str]:
    stored = normalize_snmp(extras if isinstance(extras, dict) else None)
    return stored or {"snmp_version": DEFAULT_VERSION, "snmp_community_mode": "default"}


def public_snmp(extras: Any) -> dict[str, Any]:
    """Template / API view: plain fields plus ``*_set`` flags. Secret values are never included."""
    stored = snmp_settings(extras)
    out: dict[str, Any] = {key: stored.get(key, "") for key in PLAIN_KEYS}
    for key in SECRET_KEYS:
        out[f"{key}_set"] = bool(stored.get(key))
    return out


def snmp_from_form(present: str, previous: Any = None, **values: str) -> dict[str, str] | None:
    """Posted SNMP block (Comms / monitoring). None when the form did not carry it.

    A blank password / community box keeps the saved one for the same version. Raises ValueError
    for a Custom community with nothing saved, a v3 row with no username, or a USM password under 8.
    """
    if not str(present or "").strip():
        return None
    before = normalize_snmp(previous if isinstance(previous, dict) else None)
    version = _choice(values.get("version"), VERSION_KEYS, DEFAULT_VERSION)
    same_version = before.get("snmp_version", DEFAULT_VERSION) == version
    raw: dict[str, str] = {"snmp_version": version}
    if version in {"v1", "v2c"}:
        mode = _choice(values.get("community_mode"), COMMUNITY_MODES, "default")
        raw["snmp_community_mode"] = mode
        if mode == "custom":
            community = _text({"c": values.get("community")}, "c")
            if not community and before.get("snmp_version") in {"v1", "v2c"}:
                community = before.get("snmp_community", "")
            if not community:
                raise ValueError("SNMP community = Custom needs a community string")
            raw["snmp_community"] = community
        return normalize_snmp(raw)
    user = _text({"snmp_v3_user": values.get("v3_user")}, "snmp_v3_user")
    if not user:
        raise ValueError("SNMP v3 needs a username")
    level = _choice(values.get("v3_level"), V3_LEVEL_KEYS, "noAuthNoPriv")
    raw.update(
        snmp_v3_user=user,
        snmp_v3_level=level,
        snmp_v3_auth_proto=str(values.get("v3_auth_proto") or ""),
        snmp_v3_priv_proto=str(values.get("v3_priv_proto") or ""),
    )
    for key, field, needed in (
        ("snmp_v3_auth_pass", "v3_auth_pass", level in {"authNoPriv", "authPriv"}),
        ("snmp_v3_priv_pass", "v3_priv_pass", level == "authPriv"),
    ):
        if not needed:
            continue
        secret = _text({key: values.get(field)}, key)
        if not secret and same_version:
            secret = before.get(key, "")
        if not secret:
            label = "auth" if key == "snmp_v3_auth_pass" else "privacy"
            raise ValueError(f"SNMP v3 {level} needs an {label} password")
        if len(secret) < USM_MIN_PASSWORD:
            label = "auth" if key == "snmp_v3_auth_pass" else "privacy"
            raise ValueError(f"SNMP v3 {label} password must be at least {USM_MIN_PASSWORD} characters")
        raw[key] = secret
    return normalize_snmp(raw)


def asset_auth_name(asset_id: str) -> str:
    return ASSET_AUTH_PREFIX + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (asset_id or "").lower())


def desired_auth(asset: Any) -> str:
    """snmp_exporter auth this asset's settings call for (may not be rendered yet)."""
    stored = snmp_settings(getattr(asset, "extras", None))
    version = stored.get("snmp_version", DEFAULT_VERSION)
    if version == "v3" or stored.get("snmp_community_mode") == "custom":
        return asset_auth_name(getattr(asset, "asset_id", "") or "")
    return DEFAULT_V1_AUTH if version == "v1" else DEFAULT_AUTH


def auth_spec(asset: Any) -> dict[str, Any] | None:
    """snmp.yml ``auths:`` entry for a per-asset auth; None when the shared config auth applies."""
    stored = snmp_settings(getattr(asset, "extras", None))
    version = stored.get("snmp_version", DEFAULT_VERSION)
    if version in {"v1", "v2c"}:
        if stored.get("snmp_community_mode") != "custom" or not stored.get("snmp_community"):
            return None
        return {"version": 1 if version == "v1" else 2, "community": stored["snmp_community"]}
    if not stored.get("snmp_v3_user"):
        return None
    spec: dict[str, Any] = {
        "version": 3,
        "username": stored["snmp_v3_user"],
        "security_level": stored.get("snmp_v3_level") or "noAuthNoPriv",
    }
    if stored.get("snmp_v3_auth_pass"):
        spec["auth_protocol"] = stored.get("snmp_v3_auth_proto") or "SHA"
        spec["password"] = stored["snmp_v3_auth_pass"]
    if stored.get("snmp_v3_priv_pass"):
        spec["priv_protocol"] = stored.get("snmp_v3_priv_proto") or "AES"
        spec["priv_password"] = stored["snmp_v3_priv_pass"]
    return spec


def generated_snmp_path() -> Path:
    env = os.environ.get("FORGESRE_GENERATED_DIR")
    if env:
        return Path(env) / "snmp.yml"
    data = os.environ.get("FORGESRE_DATA")
    root = Path(data) if data else Path(__file__).resolve().parents[2] / "data"
    return root / "generated" / "snmp.yml"


_rendered_cache: dict[str, Any] = {"key": None, "names": frozenset()}


def rendered_auth_names(path: Path | None = None) -> frozenset[str]:
    """``auths:`` keys in the snmp.yml snmp_exporter is running with. Empty when unreadable."""
    target = path or generated_snmp_path()
    try:
        stat = target.stat()
    except OSError:
        return frozenset()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    if _rendered_cache["key"] == key:
        return _rendered_cache["names"]
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return frozenset()
    auths = data.get("auths") if isinstance(data, dict) else None
    names = frozenset(str(name) for name in auths) if isinstance(auths, dict) else frozenset()
    _rendered_cache.update(key=key, names=names)
    return names


def effective_auth(asset: Any, rendered: frozenset[str] | None = None) -> str:
    """Auth label for SNMP HTTP SD: the desired auth once snmp.yml has it, else ``public_v2``."""
    wanted = desired_auth(asset)
    if wanted == DEFAULT_AUTH:
        return wanted
    names = rendered if rendered is not None else rendered_auth_names()
    return wanted if wanted in names else DEFAULT_AUTH


def auth_status(asset: Any) -> dict[str, Any]:
    """Asset detail view: what is saved and what snmp_exporter is actually using. No secrets."""
    view = public_snmp(getattr(asset, "extras", None))
    wanted = desired_auth(asset)
    used = effective_auth(asset)
    return {**view, "desired": wanted, "effective": used, "pending": wanted != used}
