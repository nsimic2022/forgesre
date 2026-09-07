# ForgeSRE extra NetBox config. Loaded after configuration.py
# (/etc/netbox/config/*.py). Do not put live pepper values in this file.
#
# NetBox v4.5+ hashes UI v2 API tokens with API_TOKEN_PEPPERS. Without at
# least one pepper (≥50 chars), the UI shows "API token peppers not defined".
# netboxcommunity/netbox (netbox-docker 5.0.2) maps env API_TOKEN_PEPPER_1
# into API_TOKEN_PEPPERS[1]. If that env is empty (compose interpolation miss),
# fall back to NETBOX_API_TOKEN_PEPPER from secrets/secrets.env (env_file).
from os import environ

_pepper = (
    (environ.get("API_TOKEN_PEPPER_1") or "").strip()
    or (environ.get("NETBOX_API_TOKEN_PEPPER") or "").strip()
)
if _pepper:
    API_TOKEN_PEPPERS = {1: _pepper}
