# Local LLM in ForgeSRE

How to turn on the optional **on-box** language model. ForgeSRE does not need a cloud LLM. The product works without this chapter: **ForgeRCA** (Python) always investigates first.

Companion pages: [install and config](install-config.md) · [CLI](cli.md) · [verify](verify.md) · [operator handbook §13](operator-handbook.md#13-ai-investigation-forgerca).

---

## 1. What the LLM actually does

Two names in the UI:

| Pill | Engine | When |
|---|---|---|
| **ForgeRCA** | Deterministic Python (`agents/rca/`) | Always. Facts, anomalies, hypotheses, evidence IDs, confidence score |
| **ForgeAI** | Local llama.cpp (or another OpenAI-compatible HTTP server) | Optional. Rewrites **prose only**: summary, likely cause, recommended action, extra limitations |

The model **does not**:

- SSH, run playbooks, or change the host
- Write NetBox
- Invent a second investigation engine (it sees the same sanitized evidence ForgeRCA already collected)
- Store a GGUF in git
- Require an OpenAI / Anthropic API key

Core talks HTTP to `ai.llm.url` (`POST …/chat/completions`, `GET …/models`). There is no cloud SDK in the runtime.

If the rewrite fails or times out, the incident keeps the ForgeRCA result and records why (`LLM unreachable; used ForgeRCA…`).

---

## 2. Hardware

Bundled **default** is **Qwen2.5-14B-Instruct Q4_K_M** (~8.4 GB on disk) as `model.gguf`. llama.cpp runs on **CPU** (no GPU required, nested virtualization not required). Official `Qwen/Qwen2.5-14B-Instruct-GGUF` Q4_K_M is now a 3-way split; `fetch-llm` pins the bartowski **single-file** GGUF so Compose can keep `-m /models/model.gguf`.

Lighter catalog entries (same llama.cpp, **not** Ollama):

| Catalog id | What you get | Disk | RAM | Ctx | File on disk |
|---|---|---|---|---|---|
| `qwen2.5-14b` (default) | Qwen2.5-14B-Instruct Q4_K_M | ~8.4 GB | 16 GB comfortable | **8192** | `model.gguf` |
| `qwen3-1.7b` | Qwen3-1.7B Q4_K_M (unsloth) | ~1.0 GB | 8 GB | 4096 | `Qwen3-1.7B-Q4_K_M.gguf` |
| `qwen2.5-1.5b` | Qwen2.5-1.5B-Instruct Q4_K_M | ~1.1 GB | 8 GB | 4096 | `qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| `qwen3-4b` | Qwen3-4B Q4_K_M | ~2.5 GB | 8 GB | 4096 | `Qwen3-4B-Q4_K_M.gguf` |

There is **no** Hugging Face repo `Qwen/Qwen3-1.7B-Instruct-GGUF` (401). Official `Qwen/Qwen3-1.7B-GGUF` ships **Q8_0 only**. Catalog `qwen3-1.7b` is `unsloth/Qwen3-1.7B-GGUF` → `Qwen3-1.7B-Q4_K_M.gguf`. Qwen2-1.5B Instruct Q4_K_M exists (`qwen2-1_5b-instruct-q4_k_m.gguf`); the pin is **Qwen2.5-1.5B-Instruct**.

vCPU: 4 is better than 2. Threads default to `nproc - 2` (min 2). First llama.cpp load after `up -d llm` is minutes for 14B (GGUF mmap); 1.5B / 1.7B is usually under a minute.

A **4 GB** lab VM should not run the 14B GGUF. Leave `ai.enabled: false` and use ForgeRCA only, or try `qwen3-1.7b` / `qwen2.5-1.5b` on 8 GB.

Default Compose context is **8192** (`-c ${FORGESRE_LLM_CTX:-8192}`). Switching to a light model writes `FORGESRE_LLM_CTX=4096`. Core waits **`ai.llm.timeout_seconds`** (default **90**) for one completion. Slow 14B CPU rewrites may need a higher value in `config/forgesre.yml`.

---

## 3. Choose a path

Do **not** re-run `./install.sh` on a box that already has `secrets/secrets.env`. That regenerates passwords.

### A. Existing VM (usual)

From the clone directory, on `main`:

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre fetch-llm
```

`./forgesre fetch-llm` (same as `scripts/fetch-llm.sh`):

1. Downloads the catalog GGUF into `$FORGESRE_DATA/models/` (default file `model.gguf`) if a large enough file is not already there
2. Sets `COMPOSE_PROFILES` to include `ai`
3. Writes `FORGESRE_LLM_THREADS` if missing
4. `--model ID` also writes `FORGESRE_LLM_GGUF` + `FORGESRE_LLM_CTX` and recreates **llm**
5. Sets `ai.enabled: true` and `ai.llm.mode: bundled` in `config/forgesre.yml`
6. Starts the `llm` container and recreates **Core** so it reloads YAML (first enable)

List and switch without overwriting the default file:

```bash
./forgesre fetch-llm --list
./forgesre fetch-llm --model qwen3-1.7b      # light + switch + restart llm
./forgesre fetch-llm --model qwen2.5-1.5b
./forgesre fetch-llm use qwen2.5-14b         # back to model.gguf, ctx 8192
```

Then wait until llama.cpp answers:

```bash
docker compose logs --tail=200 llm
curl -fsS http://127.0.0.1:8088/v1/models
./forgesre doctor          # llm: ok
./forgesre test
```

### B. First install with the GGUF

New VM only:

```bash
./install.sh --non-interactive --profile full-ai --port 8080
```

Same effect as `--enable-ai yes`: installer downloads the GGUF, then starts Compose with profile `ai` when `data/models/model.gguf` exists.

### C. Catalog / lighter GGUF / your own file

Compose loads **one** basename under the models volume:

```text
/models/${FORGESRE_LLM_GGUF:-model.gguf}
```

Unset `FORGESRE_LLM_GGUF` is still `-m /models/model.gguf` (the screenshot default). Default `$FORGESRE_DATA` is `./data`. Check what is already there:

```bash
./forgesre fetch-llm --list
ls -lah ./data/models/
```

The filename must be a **basename** ending in `.gguf` (no slashes). A `wget -O model.gguf` in the clone root does **not** count — write into `data/models/`.

**Try a lighter model** (keeps `model.gguf` if you already have 14B):

```bash
./forgesre fetch-llm --model qwen3-1.7b
./forgesre fetch-llm --model qwen2.5-1.5b
curl -fsS http://127.0.0.1:8088/v1/models
```

System Health → **Local LLM (llama.cpp)** shows the same catalog. Switching is host CLI (`./forgesre fetch-llm use …`) because Core does not restart Compose.

**Optional medium** — Qwen3-4B Q4_K_M (~2.5 GB):

```bash
./forgesre fetch-llm --model qwen3-4b
```

Same file as the older wget path (catalog filename, then `use`):

```bash
mkdir -p data/models
wget -O data/models/Qwen3-4B-Q4_K_M.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
./forgesre fetch-llm use qwen3-4b --offline
```

Or overwrite the default compose filename (`-m /models/model.gguf`) and enable without a catalog id:

```bash
wget -O data/models/model.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
./forgesre fetch-llm --offline
```

`--offline` skips Hugging Face; the catalog file must already exist and be large enough. It still sets `COMPOSE_PROFILES=ai`, `ai.enabled` / `ai.llm.mode: bundled` in **`config/forgesre.yml`** (not the example template), starts `llm`, and recreates Core on first enable.

Override any URL (internal mirror) while targeting a catalog id:

```bash
FORGESRE_LLM_URL='https://example.internal/models/qwen.gguf' \
  ./forgesre fetch-llm --model qwen2.5-14b
```

Download only (no Compose / YAML changes):

```bash
./forgesre fetch-llm --download-only
./forgesre fetch-llm --model qwen3-1.7b --download-only
```

Prefer an **Instruct** GGUF so the model returns JSON. If ForgeAI stays on builtin ForgeRCA with “not JSON”, `use` another catalog id and wait for llama.cpp to reload.

If the GGUF is already in place and YAML is already enabled:

```bash
docker compose --profile ai up -d --force-recreate llm
docker compose ps
docker compose logs -f llm
```

Do **not** edit `config/forgesre.example.yml` on a live box — Core reads `config/forgesre.yml`. Do not re-run `./install.sh` to add a model. Do not paste `secrets/secrets.env` into tickets.

If an older 1.5B pin was saved as `model.gguf` (~1.1 GB), move it aside before fetching 14B:

```bash
mv data/models/model.gguf data/models/qwen2.5-1.5b-instruct-q4_k_m.gguf
./forgesre fetch-llm --model qwen2.5-14b
```

### D. External OpenAI-compatible server (same host)

If you already run Ollama, vLLM, or another llama.cpp on the VM, you do **not** have to start the bundled `llm` container.

In `config/forgesre.yml`:

```yaml
ai:
  enabled: true
  llm:
    mode: external
    url: http://127.0.0.1:8088/v1    # your server; must speak /chat/completions
    model: local                     # or the exact model id
    timeout_seconds: 90
```

Leave `COMPOSE_PROFILES` without `ai` if you do not want `ghcr.io/ggml-org/llama.cpp:server`. Recreate Core:

```bash
docker compose up -d --force-recreate core
curl -fsS http://127.0.0.1:8088/v1/models
./forgesre doctor
```

Core sends a **plain** OpenAI chat body first (`temperature` 0.1, `max_tokens` 512). Extra llama.cpp `chat_template_kwargs` are only a fallback. The model name `local` / `default` is resolved via `GET /v1/models`.

ForgeSRE is not a hosted OpenAI client. Do not put cloud API keys in `secrets/secrets.env` as the product path.

---

## 4. Files that control the LLM

| File | What to set | Git |
|---|---|---|
| `data/models/*.gguf` | Weights. Default `model.gguf` (~8.4 GB); lights keep their own names | ignored |
| `.env` | `COMPOSE_PROFILES=ai`, `FORGESRE_LLM_THREADS`, `FORGESRE_DATA`, optional `FORGESRE_LLM_GGUF` / `FORGESRE_LLM_CTX` | ignored |
| `backend/app/llm_catalog.py` | Catalog ids, URLs, filenames, ctx | committed |
| `config/forgesre.yml` | `ai.enabled`, `ai.llm.mode` / `url` / `model` / `timeout_seconds` | ignored |
| `config/forgesre.example.yml` | Template only | committed |
| `docker-compose.yml` | Service `llm` under Compose **profile `ai`**, host network, port **8088** | committed |

YAML that Core actually loads:

```yaml
ai:
  enabled: true
  provider: local
  llm:
    mode: bundled          # bundled | external | disabled
    url: http://127.0.0.1:8088/v1
    model: local
    timeout_seconds: 90
  rca:
    engine: forgerca
    window_minutes: 30
    max_log_lines: 20
    max_evidence: 40
```

- `mode: disabled` or `ai.enabled: false` → Core does not call the model. ForgeRCA still runs.
- `mode: bundled` → start `docker compose --profile ai up -d llm`.
- Changing YAML requires **recreating Core** (`settings` load at process start):

```bash
docker compose up -d --force-recreate core
```

Changing Python under `agents/rca/` (including `llm.py`) requires a **image rebuild**:

```bash
docker compose build core
docker compose up -d core
docker compose ps core
```

Do not commit `config/forgesre.yml`, `.env`, or the GGUF.

---

## 5. Compose service (bundled llama.cpp)

Profile `ai` (not started until you ask):

```bash
docker compose --profile ai up -d llm
docker compose ps llm
```

What the container runs:

- Image `ghcr.io/ggml-org/llama.cpp:server`
- Host network, bind **127.0.0.1:8088** (not published to the laptop; Core on the same VM talks to it)
- `-m /models/${FORGESRE_LLM_GGUF:-model.gguf} -c ${FORGESRE_LLM_CTX:-8192} -t $FORGESRE_LLM_THREADS`
- Health: `curl -f http://127.0.0.1:8088/v1/models`

`.env`:

```bash
COMPOSE_PROFILES=ai
FORGESRE_LLM_THREADS=8
# FORGESRE_LLM_GGUF=model.gguf    # unset still means model.gguf
# FORGESRE_LLM_CTX=8192           # light models write 4096
```

`./forgesre fetch-llm` sets threads to `nproc - 2` if the variable is missing. Raise or lower it if the VM is CPU-starved (Prometheus/Grafana share the same CPUs). Recreate `llm` after changing threads:

```bash
docker compose --profile ai up -d --force-recreate llm
```

---

## 6. What happens when you click Run AI investigation

1. Core runs **ForgeRCA immediately** (`use_llm=false`) and shows the builtin report.
2. If `ai.enabled` and `ai.llm.url` are set, Core **enqueues** a background job (`./forgesre jobs`) with `use_llm=true`.
3. The worker calls llama.cpp. The model must return **JSON only** with keys:
   - `summary`
   - `likely_cause`
   - `recommended_action`
   - `limitations` (array of strings)
4. Shell-like strings in the action (`sudo`, `ssh`, `rm -`, …) are rewritten to **RECOMMENDED ACTION (not executed)**.
5. Provider becomes `forgerca-llm`. Refresh `/ai/INC-…`. ForgeAI pill goes **green**. Yellow = still running (minutes on CPU). Red = off or failed.

Alertmanager ingest also enqueues investigate; the webhook does **not** wait on the LLM.

```bash
./forgesre jobs
./forgesre demo          # first-hour HighCPU; RCA inline, rewrite queued
```

Do not mash **Run AI investigation** while a job is `running`.

---

## 7. Verify

```bash
curl -fsS http://127.0.0.1:8088/v1/models
curl -fsS http://127.0.0.1:8088/health
./forgesre doctor
./forgesre test
./forgesre logs llm
./forgesre logs core
```

| Check | Pass means |
|---|---|
| Doctor component `llm` | `GET /v1/models` returned 2xx |
| `./forgesre test` row `http.llm` | Same URL; **SKIP** if profile `ai` is off and :8088 is closed |
| Core logs | `llm` / `rca` / `exception` greps stay quiet after a rewrite |
| UI | ForgeRCA green, ForgeAI green after the job finishes |

`./forgesre test` does **not** wait for a full rewrite and does **not** send mail.

---

## 8. Advanced CLI (debug)

Use this on an **already installed** VM when ForgeAI is red, `:8088` is quiet, or a rewrite never finishes. Do **not** run `./install.sh` again. Do **not** run `docker compose down` unless you mean to stop the whole appliance.

### 8.1 Stack and the LLM container

```bash
docker ps
docker compose ps
docker compose ps core
docker compose ps llm
docker compose --profile ai up -d llm
```

Health of the llama.cpp container (name is usually `forgesre-llm-1`; prefer Compose so it still works if the project name differs):

```bash
docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'
docker compose ps -q llm | xargs -r docker inspect --format='{{json .Config.Healthcheck.Test}}'
```

Healthy looks like `"Status":"healthy"` and the test should be `curl -f http://127.0.0.1:8088/v1/models`. `starting` for several minutes after `up -d llm` is normal (GGUF load). `unhealthy` after that → logs, then GGUF size.

### 8.2 HTTP on :8088

```bash
curl -sS http://127.0.0.1:8088/v1/models
curl -sS http://127.0.0.1:8088/health
```

A JSON list with a `data[0].id` means llama.cpp is serving. Empty / connection refused → container down or still loading.

`GET /v1/chat/completions` without a body is **not** a real rewrite. Core always **POST**s. Smoke the same path the client uses (short timeout; 14B may still take a while):

```bash
curl -sS -m 30 http://127.0.0.1:8088/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'
```

If `/models` is 200 but this hangs, the model is loaded but CPU is busy — wait, watch `jobs`, do not restart in a loop.

### 8.3 Logs (llama.cpp and Core)

```bash
docker compose logs --tail=100 llm
docker compose logs -f llm
docker compose logs --tail=100 core
docker compose logs --tail=100 core | grep -iE "llm|openai|model|error|exception"
docker compose logs --tail=50 core
docker compose logs --tail=50 core | grep "/ai"
./forgesre logs llm
./forgesre logs core
```

llama.cpp should show the GGUF path `/models/${FORGESRE_LLM_GGUF:-model.gguf}` and eventually listening on `127.0.0.1:8088`. Core should show `openai-compatible` / `forgerca-llm` after a successful rewrite, not a traceback.

### 8.4 Config on disk (not secrets passwords)

```bash
grep -nE 'llm|8088|model|COMPOSE_PROFILES|FORGESRE_LLM' .env config/forgesre.yml 2>/dev/null
./forgesre config
./forgesre fetch-llm --list
ls -lh "${FORGESRE_DATA:-./data}/models/"
```

You want `COMPOSE_PROFILES` containing `ai`, `ai.enabled: true`, `ai.llm.mode: bundled` (or `external` + your URL), and the **active** GGUF large enough (see `--list`). Do not paste `secrets/secrets.env` into tickets.

### 8.5 Rebuild Core after Python / compose changes

The `core` image copies `agents/rca/llm.py` at **build** time. Editing the file on the VM with `nano` / `vi` does nothing until:

```bash
docker compose build core
docker compose up -d core
docker compose ps core
```

Same after `git pull` that touched `agents/rca/` or `docker-compose.yml`:

```bash
git checkout main
git pull origin main
./forgesre update
docker compose --profile ai up -d llm
docker compose build core
docker compose up -d core
./forgesre test
```

Inspect the client that ships in git (read-only; you do not need to change it to turn LLM on):

```bash
sed -n '1,220p' agents/rca/llm.py
sed -n '1,140p' agents/rca/engines.py
grep -n complete_json agents/rca/*.py backend/app/*.py
```

`complete_json` is the OpenAI-compatible POST. `enable_thinking` / `chat_template_kwargs` are a **fallback** only — Qwen 2.5 gets a plain body first.

Inside Core (imports / working directory):

```bash
docker compose exec -T core pwd
docker compose exec -T core ls
docker compose exec -T core python -c "import sys; print('\n'.join(sys.path))"
```

### 8.6 Do not do this on a live box

```bash
# docker compose down     # stops Core, Postgres, Prometheus, LLM — last resort
# ./install.sh            # regenerates passwords
# cp llm.py llm.py.backup # local experiment only; keep the git file as source of truth
```

---

## 9. Troubleshooting

| Symptom | What to do |
|---|---|
| `llm: disabled` / ForgeAI red | `ai.enabled` is false or `mode: disabled`. Enable YAML, recreate Core. Or run `./forgesre fetch-llm` |
| `curl :8088` connection refused | Profile not `ai`, or container still loading the GGUF. `docker compose --profile ai up -d llm` and wait; watch `logs llm` |
| Health `"Status":"unhealthy"` | GGUF missing, curl healthcheck cannot reach `:8088`. `docker inspect` the Test field; `./forgesre fetch-llm --list` |
| `/v1/models` ok, chat hangs | CPU 14B is slow or still loading layers. Follow `docker compose logs -f llm`. Do not `compose down`. Try `--model qwen3-1.7b` |
| Doctor `llm: error` after many minutes | GGUF missing/corrupt, OOM, or image pull failed. `./forgesre fetch-llm --list` |
| Rewrite never finishes | CPU 14B is slow. Check `./forgesre jobs`. Raise `timeout_seconds` (default 90; 14B CPU may need 300–600), recreate Core. Or switch to `qwen3-1.7b` / `qwen2.5-1.5b`. Lower `FORGESRE_LLM_THREADS` if the VM is thrashing |
| `LLM returned text that was not JSON` | Model ignored the schema. Builtin ForgeRCA stays. Keep Qwen Instruct; do not swap a base (non-instruct) GGUF |
| HTTP 400 from llama.cpp | Core already retries without extra template kwargs. Rebuild Core if you are on an old image |
| Hugging Face download fails | Copy the catalog filename onto the VM (scp), then `./forgesre fetch-llm use ID --offline` |
| Re-install “to add AI” wiped users | Never `./install.sh` on a live box. Use `./forgesre fetch-llm` |
| Changed `llm.py` but Core ignores it | `docker compose build core && docker compose up -d core` — the container does not bind-mount that file in production |

---

## 10. What this version does not do

- No GPU compose profile, no CUDA flags in the default `llm` service
- Health UI lists the catalog; **switching** is still `./forgesre fetch-llm use` (Core has `.env` read-only and no Docker socket)
- No cloud provider as a supported product path
- Ollama is not the product default (`ai.llm.mode: external` remains an operator YAML choice)
- AI never executes the recommended action
