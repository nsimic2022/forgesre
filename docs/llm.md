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
- Invent a second investigation engine (it sees a **compact** sanitized rewrite context — facts, CPU/mem/disk snapshots, short logs — not Prometheus matrices or full evidence JSON)
- Store a GGUF in git
- Require an OpenAI / Anthropic API key

Core talks HTTP to `ai.llm.url` (`POST …/chat/completions`, `GET …/models`). There is no cloud SDK in the runtime.

If the rewrite fails or times out, the incident keeps the ForgeRCA result and records why (`LLM unreachable; used ForgeRCA…`).

**Prompt size.** llama.cpp **prompt processing (prefill)** is not generation. Qwen3-4B on CPU is often ~25–29 tok/s for prefill; a ~6000-token user message is ~4 minutes before the first output token. `timeout_seconds: 300` then leaves almost no time to generate, and llama.cpp logs `cancel task`. ForgeSRE therefore **does not dump Prom/Loki blobs** into the LLM: `prompt_context` is capped at **5000 characters** (was 12000, which is ~3–6k tokens) and keeps name/value/unit snapshots (for example CPU 92% / mem 81% / disk 74%). Builtin ForgeRCA still stores the full facts, anomalies, evidence IDs, and PromQL on the incident. Do **not** switch to a smaller GGUF to fix a cancel — shrink was the prompt, not the model. Node exporter does not talk to the LLM (Node Exporter → Prometheus → ForgeRCA evidence → compact prompt → Qwen).

---

## 2. Hardware

Bundled default is **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB on disk). llama.cpp runs on **CPU** (no GPU required, nested virtualization not required).

| Resource | 14B Q4 (default `fetch-llm`) | 4B Q4 (lab wget, see §3.C) |
|---|---|---|
| Disk for GGUF | ~9 GB | ~2.5 GB |
| RAM with the rest of the stack | 16 GB comfortable | 8 GB is enough |
| One rewrite on CPU | 1–10 minutes | usually under a minute after load |

vCPU: 4 is better than 2. Threads default to `nproc - 2` (min 2). First llama.cpp load after `up -d llm` is minutes (GGUF mmap).

A **4 GB** lab VM should not run either GGUF. Leave `ai.enabled: false` and use ForgeRCA only.

Context window in Compose is **8192** tokens (`-c 8192`). Core waits **`ai.llm.timeout_seconds`** (default **90**, lab 4B) for one completion. Slow 14B CPU rewrites may need a higher value in `config/forgesre.yml`. A huge prompt plus CPU 4B can still need **300s** until the rewrite context is small — that is prefill cost, not a reason to change the GGUF. Live `config/forgesre.yml` is gitignored; this repo does not raise the example default back to 600.

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

1. Downloads the GGUF to `$FORGESRE_DATA/models/model.gguf` (default `./data/models/model.gguf`) if a file larger than 1 GB is not already there
2. Sets `COMPOSE_PROFILES` to include `ai`
3. Writes `FORGESRE_LLM_THREADS` if missing
4. Sets `ai.enabled: true` and `ai.llm.mode: bundled` in `config/forgesre.yml`
5. Starts the `llm` container and recreates **Core** so it reloads YAML

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

### C. Offline / your own GGUF (wget)

Compose always loads **one** file:

```text
$FORGESRE_DATA/models/model.gguf
```

Default `$FORGESRE_DATA` is `./data`. Check what is already there:

```bash
ls -lah ./data/models/
```

The filename on disk **must** be `model.gguf` (the container argument is `-m /models/model.gguf`). A `wget -O model.gguf` in the clone root does **not** count — write into `data/models/`.

**Lab / 8 GB RAM** — Qwen3-4B Q4_K_M (~2.5 GB), then enable the profile (does not regenerate secrets):

```bash
mkdir -p data/models
wget -O data/models/model.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
ls -lah data/models/model.gguf
./forgesre fetch-llm --offline
```

`--offline` skips Hugging Face; the file must already exist and be larger than 1 GB. It still sets `COMPOSE_PROFILES=ai`, `ai.enabled` / `ai.llm.mode: bundled` in **`config/forgesre.yml`** (not the example template), starts `llm`, and recreates Core.

Same download through `fetch-llm` (curl with resume) instead of wget:

```bash
FORGESRE_LLM_URL='https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf' \
  ./forgesre fetch-llm
```

If the GGUF is already in place and YAML is already enabled:

```bash
docker compose --profile ai up -d llm
docker compose ps
docker compose logs -f llm
```

Do **not** edit `config/forgesre.example.yml` on a live box — Core reads `config/forgesre.yml`. Do not re-run `./install.sh` to add a model. Do not paste `secrets/secrets.env` into tickets.

Download only (no Compose / YAML changes):

```bash
./forgesre fetch-llm --download-only
```

Override any URL (internal mirror):

```bash
FORGESRE_LLM_URL='https://example.internal/models/qwen.gguf' ./forgesre fetch-llm
```

Prefer an **Instruct** GGUF so the model returns JSON. If ForgeAI stays on builtin ForgeRCA with “not JSON”, swap the file and recreate `llm`.

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
| `data/models/model.gguf` | Weights. ~9 GB | ignored |
| `.env` | `COMPOSE_PROFILES=ai` (or `ai,mailbox`), `FORGESRE_LLM_THREADS`, `FORGESRE_DATA` | ignored |
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
- `-m /models/model.gguf -c 8192 -t $FORGESRE_LLM_THREADS`
- Health: `curl -f http://127.0.0.1:8088/v1/models`

`.env`:

```bash
COMPOSE_PROFILES=ai
FORGESRE_LLM_THREADS=8
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

llama.cpp should show the GGUF path `/models/model.gguf` and eventually listening on `127.0.0.1:8088`. Core should show `openai-compatible` / `forgerca-llm` after a successful rewrite, not a traceback.

### 8.4 Config on disk (not secrets passwords)

```bash
grep -nE 'llm|8088|model|COMPOSE_PROFILES|FORGESRE_LLM' .env config/forgesre.yml 2>/dev/null
./forgesre config
ls -lh "${FORGESRE_DATA:-./data}/models/model.gguf"
```

You want `COMPOSE_PROFILES` containing `ai`, `ai.enabled: true`, `ai.llm.mode: bundled` (or `external` + your URL), and a GGUF larger than 1 GB. Do not paste `secrets/secrets.env` into tickets.

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
sed -n '1,250p' agents/rca/llm.py
sed -n '1,160p' agents/rca/engines.py
grep -n complete_json agents/rca/*.py backend/app/*.py
grep -n PROMPT_CONTEXT_MAX_CHARS agents/rca/llm.py
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
| Health `"Status":"unhealthy"` | GGUF missing, curl healthcheck cannot reach `:8088`. `docker inspect` the Test field; `ls -lh data/models/model.gguf` |
| `/v1/models` ok, chat hangs | CPU 14B is slow or still loading layers. Follow `docker compose logs -f llm`. Do not `compose down` |
| Doctor `llm: error` after many minutes | GGUF missing/corrupt (file smaller than 1 GB), OOM, or image pull failed. `ls -lh data/models/model.gguf` |
| Rewrite never finishes | CPU 14B is slow. Check `./forgesre jobs`. Raise `timeout_seconds` (default 90; 14B CPU may need 300–600), recreate Core. Lower `FORGESRE_LLM_THREADS` if the VM is thrashing |
| llama.cpp `cancel task` after a long prompt | Prefill ate the timeout. Watch `docker compose logs -f llm` for prompt tokens. After `git pull` + `./forgesre update`, the LLM user message is compact (~5000 chars). Do not swap to a smaller model first. Leave `timeout_seconds: 300` if you already set it; 90s is enough once prompt tokens drop |
| `LLM returned text that was not JSON` | Model ignored the schema. Builtin ForgeRCA stays. Keep Qwen Instruct; do not swap a base (non-instruct) GGUF |
| HTTP 400 from llama.cpp | Core already retries without extra template kwargs. Rebuild Core if you are on an old image |
| Hugging Face download fails | Copy `model.gguf` onto the VM (scp), then `./forgesre fetch-llm --offline` |
| Re-install “to add AI” wiped users | Never `./install.sh` on a live box. Use `./forgesre fetch-llm` |
| Changed `llm.py` but Core ignores it | `docker compose build core && docker compose up -d core` — the container does not bind-mount that file in production |

---

## 10. What this version does not do

- No GPU compose profile, no CUDA flags in the default `llm` service
- No multi-model picker in the UI
- No cloud provider as a supported product path
- AI never executes the recommended action
