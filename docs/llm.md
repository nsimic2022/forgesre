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

Bundled default is **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB on disk). llama.cpp runs on **CPU** (no GPU required, nested virtualization not required).

| Resource | Practical floor for 14B Q4 + the rest of the appliance |
|---|---|
| Disk | 20 GB free (GGUF + images). Installer preflight uses 20 GB when AI is on |
| RAM | 16 GB is comfortable. 8 GB often works but the rewrite is slow and may OOM |
| vCPU | 4 better than 2. Threads default to `nproc - 2` (min 2) |
| Time | First llama.cpp load after `up -d llm` is several minutes. One rewrite can take **1–10 minutes** on CPU |

A 4 GB lab VM should **not** run the 14B GGUF. Leave `ai.enabled: false` and use ForgeRCA only.

Context window in Compose is **8192** tokens (`-c 8192`). Core waits **`ai.llm.timeout_seconds`** (default **600**) for one completion.

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

### C. Offline / your own GGUF

Place a **single-file Instruct GGUF** at:

```text
$FORGESRE_DATA/models/model.gguf
```

Then:

```bash
./forgesre fetch-llm --offline
```

`--offline` refuses to hit the network; the file must already be there and larger than 1 GB.

Download only (no Compose / YAML changes):

```bash
./forgesre fetch-llm --download-only
```

Override the Hugging Face URL (or point curl at an internal mirror):

```bash
FORGESRE_LLM_URL='https://example.internal/models/qwen.gguf' ./forgesre fetch-llm
```

The Compose service always mounts that directory **read-only** as `/models` and passes `-m /models/model.gguf`. Rename the file to `model.gguf`.

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
    timeout_seconds: 600
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
    timeout_seconds: 600
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

On an already installed VM:

```bash
docker compose ps
docker compose ps llm
docker compose --profile ai up -d llm

docker compose logs --tail=200 llm
docker compose logs --tail=100 core
docker compose logs --tail=100 core | grep -iE "llm|rca|error|exception"
docker compose logs --tail=50 core | grep "/ai"
docker compose logs -f core

docker compose exec -T core python -c "import sys; print('\n'.join(sys.path))"

./forgesre config          # confirm ai.enabled / llm.url
```

After `git pull` that touched `agents/rca/llm.py` or `docker-compose.yml`:

```bash
git checkout main
git pull origin main
./forgesre update
docker compose --profile ai up -d llm
docker compose build core
docker compose up -d core
./forgesre test
```

---

## 9. Troubleshooting

| Symptom | What to do |
|---|---|
| `llm: disabled` / ForgeAI red | `ai.enabled` is false or `mode: disabled`. Enable YAML, recreate Core. Or run `./forgesre fetch-llm` |
| `curl :8088` connection refused | Profile not `ai`, or container still loading the GGUF. `docker compose --profile ai up -d llm` and wait; watch `logs llm` |
| Doctor `llm: error` after many minutes | GGUF missing/corrupt (file smaller than 1 GB), OOM, or image pull failed. `ls -lh data/models/model.gguf` |
| Rewrite never finishes | CPU 14B is slow. Check `./forgesre jobs`. Raise `timeout_seconds` (already 600), recreate Core. Lower `FORGESRE_LLM_THREADS` if the VM is thrashing |
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
