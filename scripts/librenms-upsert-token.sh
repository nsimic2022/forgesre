#!/usr/bin/env bash
# Mint LIBRENMS_API_TOKEN once the bundled UI answers. Never rotates a non-empty token.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

secret_val() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v k="$key" '$0 ~ "^"k"=" {print substr($0, index($0,"=")+1)}' "$file" | tail -1 | tr -d '\r'
}

ensure_nonempty() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    printf '%s=%s\n' "$key" "$value" > "$file"
    return 0
  fi
  if grep -qE "^${key}=.+" "$file"; then
    return 0
  fi
  # Quote so Compose / librenms_dotenv keep the id|secret pipe (scripts never source this file).
  local rendered
  rendered="$(printf '%s="%s"' "$key" "$value")"
  if grep -qE "^${key}=" "$file"; then
    # Avoid sed | in the token: rewrite via a temp line replace in python-less awk.
    awk -v k="$key" -v line="$rendered" 'index($0, k "=")==1 {print line; next} {print}' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
    return 0
  fi
  printf '%s\n' "$rendered" >> "$file"
}

token="$(secret_val "$ROOT/secrets/secrets.env" LIBRENMS_API_TOKEN)"
if [[ -n "$token" ]]; then
  echo "LibreNMS API token already set (not rotated)."
  exit 0
fi

if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi

echo "Waiting for LibreNMS on :8000 (first boot migrates MariaDB)..."
ok=0
for _i in $(seq 1 45); do
  if curl -fsS -m 2 "http://127.0.0.1:8000/login" >/dev/null 2>&1; then
    echo "LibreNMS UI is up."
    ok=1
    break
  fi
  sleep 4
done
if [[ "$ok" -ne 1 ]]; then
  echo "LibreNMS UI is not answering yet. Create an API token in LibreNMS → API Access,"
  echo "set LIBRENMS_API_TOKEN in secrets/secrets.env, then recreate core."
  echo "Logs: docker compose logs --tail=80 librenms"
  exit 0
fi

pass="$(secret_val "$ROOT/secrets/secrets.env" LIBRENMS_ADMIN_PASSWORD)"
if [[ -z "$pass" ]]; then
  pass="$(openssl rand -hex 12)"
  ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_ADMIN_PASSWORD "$pass"
fi

# lnms and the app refuse to run as root inside the image: run as the librenms user.
add_out="$("${DC[@]}" exec -T --user librenms librenms lnms user:add forgesre --password="$pass" --role=admin --email=forgesre@localhost 2>&1)" || {
  case "$add_out" in
    *exists*|*taken*) ;;  # the user is there from an earlier run
    *) echo "LibreNMS user forgesre not added: $(printf '%s' "$add_out" | tail -2 | tr '\n' ' ')" ;;
  esac
}

php_code='require "/opt/librenms/vendor/autoload.php";
$app = require "/opt/librenms/bootstrap/app.php";
$kernel = $app->make(Illuminate\Contracts\Console\Kernel::class);
$kernel->bootstrap();
$user = \App\Models\User::where("username", "forgesre")->first() ?: \App\Models\User::first();
if (!$user) { fwrite(STDERR, "no-user\n"); exit(2); }
if (method_exists(\App\Models\ApiToken::class, "generate")) {
  echo \App\Models\ApiToken::generate($user, "ForgeSRE Core discovery");
  exit(0);
}
$token = bin2hex(random_bytes(16));
$row = new \App\Models\ApiToken();
$uid = $user->user_id ?? $user->id;
$row->user_id = $uid;
// LibreNMS compares the X-Auth-Token header with token_hash as stored: keep the token itself there.
if (\Illuminate\Support\Facades\Schema::hasColumn("api_tokens", "token_hash")) {
  $row->token_hash = $token;
} else {
  fwrite(STDERR, "no-hash-column\n");
  exit(3);
}
$row->description = "ForgeSRE Core discovery";
if (\Illuminate\Support\Facades\Schema::hasColumn("api_tokens", "disabled")) {
  $row->disabled = 0;
}
$row->save();
echo $token;
'

new=""
errf="$(mktemp)"
if new="$("${DC[@]}" exec -T --user librenms librenms php -r "$php_code" 2>"$errf")"; then
  :
else
  new=""
fi
new="$(printf '%s' "$new" | tr -d '\r\n[:space:]')"
if [[ ${#new} -lt 16 ]]; then
  echo "Could not mint LibreNMS API token: $(tail -3 "$errf" | tr '\n' ' ')"
  echo "Create one in LibreNMS → API Access, then: ./forgesre settings set-secret LIBRENMS_API_TOKEN"
  echo "Or retry: ./forgesre librenms-token"
  rm -f "$errf"
  exit 0
fi
rm -f "$errf"

ensure_nonempty "$ROOT/secrets/secrets.env" LIBRENMS_API_TOKEN "$new"
echo "LibreNMS API token written."
exit 0
