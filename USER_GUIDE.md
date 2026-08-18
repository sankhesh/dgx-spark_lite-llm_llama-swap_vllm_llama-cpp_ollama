# DGX Spark — Local Model Server User Guide

A practical guide to using this stack as a local LLM server on the DGX Spark
(hostname: **`spearca`**). Covers how model swapping works, how to connect
editors and CLIs, the monitoring dashboards, and day-to-day operations.

> **Secrets note:** this file intentionally references credentials by their
> `.env` variable names (e.g. `GRAFANA_ADMIN_PASSWORD`) rather than printing
> the actual values, because it is tracked in git. Look up the live values with
> `grep VAR_NAME .env` on the host.

---

## 1. Endpoints at a glance

Replace `spearca` with `localhost` if you're on the box itself.

| Service       | URL                          | Auth                                                        | Purpose                                             |
|---------------|------------------------------|-------------------------------------------------------------|-----------------------------------------------------|
| **LiteLLM API**   | `http://spearca:14000/v1`    | none (open) — dummy key accepted                            | The unified endpoint you point clients at           |
| LiteLLM UI    | `http://spearca:14000/ui`    | `admin` / `LITELLM_UI_PASSWORD`                             | Chat playground, model list, usage logs             |
| llama-swap UI | `http://spearca:28080/ui/`   | none                                                        | Watch/manually load/evict models, tail model logs   |
| Grafana       | `http://spearca:3000`        | `admin` / `GRAFANA_ADMIN_PASSWORD`                          | Usage + GPU dashboards                               |
| Prometheus    | `http://spearca:9090`        | none                                                        | Raw metrics / query UI                              |
| Portainer     | `https://spearca:9444`       | (set on first login)                                        | Container management UI (host port, `.env` `PORTAINER_PORT`) |

### Available models

Served through LiteLLM → llama-swap → (llama.cpp or vLLM):

| Model name (use as `model` in API calls)          | Backend   | Cold-load    | Notes                                 |
|---------------------------------------------------|-----------|--------------|---------------------------------------|
| `Meta-Llama-3-8B-Instruct`                        | llama.cpp | ~5–15 s      | Great default                         |
| `Phi-4`                                           | llama.cpp | ~15–30 s     | 14B-class general                     |
| `Meta-Llama-3.1-70B-Instruct`                     | llama.cpp | ~30–60 s     | Bigger, first call slower             |
| `Qwen2.5-Coder-32B-Instruct`                      | llama.cpp | ~20 s        | Fast code chat (**no** tool-calling)  |
| `Qwen3-Coder-30B-tools`                           | vLLM      | **~8–10 min**| MoE coder with **structured tool-calls** for agentic editing (§3) |
| `gpt-oss-120b`                                    | vLLM      | **~8–10 min**| OpenAI open-weight MoE; strong reasoning + **tool-calls** (§3)     |
| `vtk-rag`                                          | proxy     | (loads Qwen2.5) | RAG Q&A over the VTK repo, no tools (§3/§4) |
| `vtk-rag-agent`                                    | proxy     | (loads Qwen3-tools) | RAG over VTK **+** agentic tool-calling (§3) |

> **Mostly a llama.cpp stack**, with one deliberate vLLM exception:
> `Qwen3-Coder-30B-tools`. llama.cpp (and Qwen2.5-Coder via vLLM's hermes parser)
> don't reliably emit structured `tool_calls` in auto mode, so codecompanion's
> agentic editing needs Qwen3-Coder with vLLM's dedicated `qwen3_coder` parser.
> Everything else stays on llama.cpp for fast loads; use the `-tools` model
> **only** when you need agent tools — its cold-load is minutes (its `ttl` is
> 1 h to avoid repeated reloads within a session), though it's a fast MoE once
> warm.

List them live:

```bash
curl http://spearca:14000/v1/models
```

---

## 2. How model swapping works

Swapping is **automatic and request-driven** — you never manually pick a model
for normal use:

1. A client sends a request naming a `model` (e.g. `Meta-Llama-3-8B-Instruct`).
2. LiteLLM forwards it to **llama-swap**.
3. llama-swap checks if that model's container is running. If not, it spawns it
   on demand (`docker run`), waits for it to become healthy, then proxies the
   request. It **evicts** the previously loaded model first, because this stack
   is configured to keep only one model resident at a time (128 GB unified
   memory is shared with the system).
4. Idle models are torn down automatically after their `ttl` (see
   `llama-swap/config.yaml`).

### First-load latency

llama.cpp (GGUF) models cold-load in seconds (~5–60 s depending on size); the
first request after an idle eviction pays that, and everything after is
sub-second until the model is evicted again (`ttl`).

> ⚠️ **The stack loads one model at a time.** A cold load briefly blocks other
> requests until the model is ready. For llama.cpp that's seconds, so it's
> rarely noticeable. `healthCheckTimeout` in `llama-swap/config.yaml` caps the
> wait at **300 s** so a genuinely stuck load fails fast instead of wedging the
> stack; don't raise it back up.

If a model ever gets stuck loading and jams the stack, clear it with:

```bash
docker compose up -d --force-recreate llama-swap
```

### Watching / controlling swaps

- **Dashboard:** `http://spearca:28080/ui/` — shows every model, its load state,
  and lets you manually **start / stop / swap** and tail logs.
- **CLI:**
  ```bash
  curl http://spearca:28080/v1/models          # states (loaded/unloaded)
  docker ps --filter name=llamacpp             # running model containers
  ```

---

## 3. Editor integration — codecompanion.nvim

Point codecompanion at the LiteLLM endpoint using its **`openai_compatible`**
adapter (LiteLLM speaks the OpenAI API). Below is your existing config with a
`dgx_spark` adapter merged in. The Claude Code / Copilot adapters are kept so
you can switch between cloud and local by changing the `adapter`/`model` lines
under `interactions`.

> ⚠️ The first request after an idle period cold-loads the model (seconds for
> these llama.cpp models). If codecompanion ever seems to hang on the first
> message, that's the load — subsequent messages are fast. Also check the chat
> buffer's model picker: a model chosen there overrides the config default.

```lua
-- lua/plugins/codecompanion.lua

return {
  'olimorris/codecompanion.nvim',
  dependencies = {
    'nvim-lua/plenary.nvim',
    'nvim-treesitter/nvim-treesitter',
    'folke/snacks.nvim',
  },
  config = function()
    require('codecompanion').setup({
      interactions = {
        chat = {
          -- Local DGX Spark (llama.cpp — fast, reliable):
          adapter = 'dgx_spark',
          model = 'Meta-Llama-3-8B-Instruct',
          -- Cloud fallbacks:
          -- adapter = 'claude_code',
          -- model = 'haiku',
        },
        inline = {
          -- Local (llama.cpp).
          adapter = 'dgx_spark',
          model = 'Meta-Llama-3-8B-Instruct',
          -- adapter = 'copilot',
          -- model = 'gemini-3.1-pro',
        },
      },
      adapters = {
        -- HTTP (OpenAI-compatible) adapters:
        http = {
          dgx_spark = function()
            return require('codecompanion.adapters').extend('openai_compatible', {
              env = {
                -- Use your Tailscale/LAN hostname or IP for the DGX Spark.
                url = 'http://spearca:14000',
                -- Auth is disabled on the proxy; any non-empty value works.
                api_key = 'sk-dgx-local',
                -- LiteLLM exposes the OpenAI API under /v1
                chat_url = '/v1/chat/completions',
                models_endpoint = '/v1/models',
              },
              schema = {
                model = {
                  -- Default model. Other picks: Phi-4, Meta-Llama-3.1-70B-Instruct.
                  default = 'Meta-Llama-3-8B-Instruct',
                },
              },
            })
          end,
        },
        -- ACP adapters (unchanged):
        acp = {
          claude_code = function()
            local home = vim.fn.expand('~')
            local file_path = vim.fn.fnamemodify(home .. '/.claude_code_apitoken', ':p')

            local token = ''
            local f = io.open(file_path, 'r')
            if f then
              token = f:read('*a'):gsub('%s+', '')
              f:close()
            else
              vim.notify('Could not find Claude Code token at ' .. file_path, vim.log.levels.WARN)
            end

            return require('codecompanion.adapters').extend('claude_code', {
              env = {
                CLAUDE_CODE_OAUTH_TOKEN = token,
              },
            })
          end,
        },
      },
    })

    vim.keymap.set(
      { 'n', 'v' },
      '<leader>a',
      '<cmd>CodeCompanionActions<cr>',
      { noremap = true, silent = true, desc = 'Code Companion Actions' }
    )
    vim.keymap.set(
      'n',
      '<leader>c',
      '<cmd>CodeCompanionChat Toggle<cr>',
      { noremap = true, silent = true, desc = 'Code Companion Chat' }
    )
    vim.keymap.set(
      'v',
      '<leader>c',
      '<cmd>CodeCompanionChat Add<cr>',
      { noremap = true, silent = true, desc = 'Add selected text to the chat buffer' }
    )
  end,
}
```

**Notes**
- `spearca` must resolve from your Mac. If it doesn't, use the DGX's LAN IP or a
  Tailscale name/IP in the `url`.
- Adapter type location (`adapters.http` vs a flat `adapters` table) can vary
  slightly between codecompanion versions. If `dgx_spark` isn't picked up, move
  the `dgx_spark = function() ... end` block up one level (directly under
  `adapters`) and keep `acp` as its own sub-table.
- To split local/cloud: leave `chat` on a cloud adapter and set only `inline` to
  `dgx_spark`, or vice-versa.

### Agentic editing (tools) — use the `-tools` model

codecompanion's agent tools (create/edit files, `@editor`, etc.) only run when
the server returns **structured `tool_calls`**. The llama.cpp GGUF models here
do **not** — they emit the call as plain text, so codecompanion just prints it
and nothing happens. Two vLLM models return proper `tool_calls` in auto mode, so
agent tools work:
- **`Qwen3-Coder-30B-tools`** — coder-focused MoE, vLLM's dedicated `qwen3_coder`
  parser. Faster (smaller). (Qwen2.5-Coder was tried first but only did *forced*
  tool calls, not the auto mode codecompanion uses — hence Qwen3-Coder.)
- **`gpt-oss-120b`** — OpenAI's open-weight MoE, stronger general reasoning,
  vLLM's `openai` (harmony) tool parser. Bigger/slower but more capable.

Select one for agent/editing work:

```lua
-- for a tools/agent chat, or via the chat buffer model picker:
model = 'Qwen3-Coder-30B-tools'   -- or 'gpt-oss-120b'
```

Notes:
- **Cold-load is minutes** (safetensors + one-time MoE-kernel compile, cached
  after). The first agent request after idle waits for the load; it stays warm
  for 1 h (`ttl`) after, and it's a fast MoE (~3B active) once loaded.
- Keep fast llama.cpp models (`Meta-Llama-3-8B-Instruct`, etc.) as your default
  for plain chat/inline; switch to `-tools` only when you need agent tools.
- `vtk-rag` is retrieval **Q&A only** (no tools) on the fast Qwen2.5-Coder GGUF.
  For agentic editing *with* VTK context, use **`vtk-rag-agent`** — same
  retrieval, but it forwards tools to `Qwen3-Coder-30B-tools`, so it returns
  structured `tool_calls` grounded in VTK. (Trade-off: it runs the slow vLLM
  tools model, so first use cold-loads for minutes.) Both are served by their
  own `rag-proxy` / `rag-proxy-agent` container over the same VTK index.

Quick check that the endpoint returns structured tool_calls:

```bash
curl -s http://spearca:14000/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"Qwen3-Coder-30B-tools","messages":[{"role":"user","content":"create /tmp/x.txt with hi"}],
       "tools":[{"type":"function","function":{"name":"create_file","parameters":{"type":"object",
       "properties":{"filepath":{"type":"string"},"content":{"type":"string"}}}}}],"tool_choice":"auto"}' \
  | python3 -c "import json,sys;print('tool_calls:', json.load(sys.stdin)['choices'][0]['message'].get('tool_calls'))"
```

---

## 4. Monitoring — Grafana + Prometheus

Two always-on containers (`prometheus`, `grafana`) collect and visualize:

- **LiteLLM metrics** (enabled via `callbacks: ["prometheus"]` in
  `LiteLLM/config.yaml`): requests/min, tokens/min, p95 latency, and failed
  requests — **broken down by model**.
- **llama-swap hardware metrics**: GPU utilization, VRAM, power draw, and
  GPU/VRAM temperature.

**Open it:** `http://spearca:3000` → login `admin` / `GRAFANA_ADMIN_PASSWORD`
→ dashboard **"DGX Spark — Model Server"** (auto-provisioned).

Config lives in `monitoring/`:

```
monitoring/
├── prometheus.yml                              # scrape targets (litellm, llama-swap)
└── grafana/
    ├── provisioning/datasources/datasource.yml # Prometheus datasource
    ├── provisioning/dashboards/dashboards.yml  # dashboard auto-loader
    └── dashboards/dgx-spark.json               # the dashboard itself
```

To edit the dashboard: change it in the Grafana UI, then **Share → Export →
Save to file** and overwrite `monitoring/grafana/dashboards/dgx-spark.json`
(it re-provisions on restart). Prometheus history persists in the
`prometheus_data` volume; Grafana state in `grafana_data`.

Sanity checks:

```bash
curl -s http://spearca:9090/api/v1/targets | grep -o '"health":"[a-z]*"'   # both should be "up"
curl -sL http://spearca:14000/metrics | grep '^litellm_total_tokens'
```

---

## 5. Using it from a terminal (CLI clients)

LiteLLM exposes **both** the OpenAI API and the Anthropic Messages API, so
several CLIs work out of the box.

### Claude Code CLI, pointed at your own hardware

```bash
export ANTHROPIC_BASE_URL=http://spearca:14000
export ANTHROPIC_API_KEY=sk-dgx-local          # dummy; auth is disabled
claude --model Qwen3-Coder-30B-tools           # or gpt-oss-120b, Meta-Llama-3-8B-Instruct, …
```

> ⚠️ **Claude Code only works with the direct model backends, not the RAG
> models.** Claude Code calls LiteLLM's `/v1/messages?beta`, which LiteLLM
> forwards to the backend's **`/v1/responses`** API. vLLM and llama.cpp
> implement that, so `Qwen3-Coder-30B-tools`, `gpt-oss-120b`, `Meta-Llama-*`,
> etc. all work. But `vtk-rag` / `vtk-rag-agent` are served by the small
> `rag-proxy`, which only speaks `/chat/completions` — so Claude Code errors with
> *"model may not exist"* for those. For **RAG in an agent**, use an OpenAI-
> `/chat/completions` client instead (codecompanion, aider, `llm`, curl) with
> `vtk-rag-agent`; use Claude Code with a direct tools model when you don't need
> VTK retrieval.

### aider (git-aware coding agent)

```bash
aider \
  --openai-api-base http://spearca:14000/v1 \
  --openai-api-key sk-dgx-local \
  --model openai/Qwen2.5-Coder-32B-Instruct
```

### Simon Willison's `llm` (general-purpose, scriptable)

```bash
llm keys set dgx --value sk-dgx-local
llm -m openai/Meta-Llama-3.1-70B-Instruct \
  -o api_base http://spearca:14000/v1 \
  "explain this stack trace"
```

### Plain curl

```bash
# OpenAI style
curl http://spearca:14000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Meta-Llama-3-8B-Instruct","messages":[{"role":"user","content":"hello"}]}'

# Anthropic Messages style
curl http://spearca:14000/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"Meta-Llama-3-8B-Instruct","max_tokens":100,"messages":[{"role":"user","content":"hello"}]}'
```

---

## 6. Operations

```bash
cd /home/sankhesh/Projects/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama

docker compose ps                    # status of all services
docker compose up -d                 # start everything
docker compose restart litellm       # apply LiteLLM/config.yaml changes
docker compose restart llama-swap    # apply llama-swap/config.yaml changes
docker compose logs -f litellm       # follow logs
```

### Adding a model (llama.cpp / GGUF)

1. Download the GGUF under `${LLM_ROOT_PATH}` (e.g. `gguf/…`).
2. Add a model block in `llama-swap/config.yaml` — copy an existing entry and
   fix the `-m /models/…gguf` path, the `-v host:/models/...` mount, and the
   `--alias` / container name.
3. Add a matching entry to `model_list` in `LiteLLM/config.yaml`
   (`api_base: http://llama-swap:8080/v1`).
4. `docker compose restart llama-swap litellm`.

### Adding more vLLM models

The stack is mostly llama.cpp, with vLLM used where it's needed (currently
`Qwen3-Coder-30B-tools`, for structured tool-calling — see §3). To add another
safetensors model, or one needing a specific vLLM feature (a tool-call parser, a
quant kernel, etc.):

1. The `vllm-spark` image already exists. If you ever need to rebuild it:
   `docker build -t $REGISTRY/$IMAGE_NAMESPACE/vllm-spark:latest -f vllm/vllm.Dockerfile vllm && docker push …`.
2. Download the safetensors under `${LLM_ROOT_PATH}/vllm/…`, then copy the
   `Qwen3-Coder-30B-tools` block in `llama-swap/config.yaml` and adjust the
   `-v …:/model` path, `--served-model-name`, and vLLM flags. Keep the
   `-v ${LLM_ROOT_PATH}/.vllm-cache:/root/.cache` mount for faster reloads.
3. Add a matching `model_list` entry in `LiteLLM/config.yaml`, then
   `docker compose restart litellm`.
4. Expect **multi-minute cold-loads**. For a model you want always hot, run it
   as a dedicated persistent service instead of swap-managed — it pins its
   memory (a 32B bf16 ≈ 65 GiB) for as long as it runs.

> **MoE OOM gotcha (learned the hard way):** MoE models make FlashInfer JIT-
> compile CUTLASS kernels for the GB10 (sm_121) on first load. With a high
> `--gpu-memory-utilization`, the parallel `nvcc` compile gets OOM-killed
> (exit 137, "Ninja build failed"). The `Qwen3-Coder-30B-tools` entry works
> around it with `--gpu-memory-utilization 0.65` (headroom for the compiler)
> plus `-e MAX_JOBS=2 -e NVCC_THREADS=1` (fewer parallel `nvcc` jobs). The
> compile is cached in `.vllm-cache`, so it's a one-time cost. Copy those
> settings for any other MoE model.
>
> Because `healthCheckTimeout` is global and must cover this slow load, it's set
> to 900 s in `llama-swap/config.yaml` — a stuck load can block the queue that
> long; clear it with `docker compose up -d --force-recreate llama-swap`.
>
> **gpt-oss / harmony gotcha:** gpt-oss models use OpenAI's "harmony" encoding,
> whose vocab (`o200k_base.tiktoken`) vLLM tries to *download at runtime* — which
> fails in-container (TLS). Fix: the vocab is pre-fetched to
> `${LLM_ROOT_PATH}/.tiktoken/` and the `gpt-oss-120b` entry sets
> `-e TIKTOKEN_ENCODINGS_BASE=/tiktoken` with that dir mounted, so it loads
> offline. Its MXFP4 weights load via vLLM's **MARLIN** backend (prebuilt, no
> JIT-compile) and its tool parser is `openai`.

### Ollama

Ollama is running but has **no models pulled** by default:

```bash
docker exec ollama ollama pull llama3.1:8b
docker exec ollama ollama list
```

---

## 7. Security

Current posture is **open, LAN-trusted**:

- The LiteLLM API has no key enforcement (`LITELLM_MASTER_KEY` is commented out).
- Grafana / Prometheus / LiteLLM all bind `0.0.0.0`.

This is fine on a trusted home network. **Before exposing `spearca` beyond your
LAN**, at minimum:

- Uncomment `LITELLM_MASTER_KEY` in the `litellm` service env (docker-compose.yml)
  and require `sk-*` keys on every call.
- Put the stack behind a reverse proxy with TLS + auth, or restrict access with
  a firewall / Tailscale ACLs.
- Rotate the `.env` secrets (they were generated during setup).
