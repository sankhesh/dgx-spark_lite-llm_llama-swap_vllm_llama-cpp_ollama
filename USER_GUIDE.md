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
| Portainer     | `https://spearca:9443`       | (set on first login)                                        | Container management UI                             |

### Available models

Served through LiteLLM → llama-swap → (llama.cpp or vLLM):

| Model name (use as `model` in API calls)          | Backend   | Notes                                  |
|---------------------------------------------------|-----------|----------------------------------------|
| `Meta-Llama-3-8B-Instruct`                        | llama.cpp | Small, fast — good default chat        |
| `Phi-4`                                           | llama.cpp | 14B-class general                      |
| `Meta-Llama-3.1-70B-Instruct`                     | llama.cpp | Large, slower to load                  |
| `Qwen3.5-35B-A3B-Uncensored-HauhauCS-Aggressive`  | llama.cpp | MoE                                    |
| `NVIDIA-Nemotron-3-Nano-4B-FP8`                   | vLLM      | FP8                                    |
| `Qwen2-7B`                                         | vLLM      |                                        |
| `Qwen3-Coder-Next-FP8-Dynamic`                    | vLLM      | **Best for coding / inline edits**     |

List them live:

```bash
curl http://spearca:14000/v1/models
```

---

## 2. How model swapping works

Swapping is **automatic and request-driven** — you never manually pick a model
for normal use:

1. A client sends a request naming a `model` (e.g. `Qwen3-Coder-Next-FP8-Dynamic`).
2. LiteLLM forwards it to **llama-swap**.
3. llama-swap checks if that model's container is running. If not, it spawns it
   on demand (`docker run`), waits for it to become healthy, then proxies the
   request. It **evicts** the previously loaded model first, because this stack
   is configured to keep only one model resident at a time (128 GB unified
   memory is shared with the system).
4. Idle models are torn down automatically after their `ttl` (see
   `llama-swap/config.yaml`).

### First-load latency

- **llama.cpp (GGUF)** models load in seconds.
- **vLLM** models are slow *on their very first load ever* (~5–6 min) because
  vLLM JIT-compiles and autotunes CUDA/Triton kernels for the GB10 architecture.
  A persistent cache at `${LLM_ROOT_PATH}/.vllm-cache` (mounted into each vLLM
  container at `/root/.cache`) stores these results, cutting subsequent cold
  loads to ~2 min. The remaining time is CUDA-graph capture, which vLLM redoes
  per process regardless of caching. Once warm, responses are sub-second.

### Watching / controlling swaps

- **Dashboard:** `http://spearca:28080/ui/` — shows every model, its load state,
  and lets you manually **start / stop / swap** and tail logs.
- **CLI:**
  ```bash
  curl http://spearca:28080/v1/models          # states (loaded/unloaded)
  docker ps --filter name=vllm --filter name=llamacpp   # running model containers
  ```

---

## 3. Editor integration — codecompanion.nvim

Point codecompanion at the LiteLLM endpoint using its **`openai_compatible`**
adapter (LiteLLM speaks the OpenAI API). Below is your existing config with a
`dgx_spark` adapter merged in. The Claude Code / Copilot adapters are kept so
you can switch between cloud and local by changing the `adapter`/`model` lines
under `interactions`.

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
          -- Local DGX Spark:
          adapter = 'dgx_spark',
          model = 'Qwen3.5-35B-A3B-Uncensored-HauhauCS-Aggressive',
          -- Cloud fallbacks:
          -- adapter = 'claude_code',
          -- model = 'haiku',
        },
        inline = {
          -- Local coding model is a great fit for inline edits:
          adapter = 'dgx_spark',
          model = 'Qwen3-Coder-Next-FP8-Dynamic',
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
                  default = 'Qwen3-Coder-Next-FP8-Dynamic',
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
claude --model Qwen3-Coder-Next-FP8-Dynamic
```

### aider (git-aware coding agent)

```bash
aider \
  --openai-api-base http://spearca:14000/v1 \
  --openai-api-key sk-dgx-local \
  --model openai/Qwen3-Coder-Next-FP8-Dynamic
```

### Simon Willison's `llm` (general-purpose, scriptable)

```bash
llm keys set dgx --value sk-dgx-local
llm -m openai/Qwen3-Coder-Next-FP8-Dynamic \
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

### Adding a model

1. Download it under `${LLM_ROOT_PATH}` (GGUF → `gguf/…` or `ollama/…`;
   safetensors → `vllm/…`).
2. Add a model block in `llama-swap/config.yaml` (copy an existing entry of the
   same backend and fix the `-m` / `-v … :/model` path + name). vLLM entries
   should include `-v ${LLM_ROOT_PATH}/.vllm-cache:/root/.cache` for fast reloads.
3. Add a matching entry to `model_list` in `LiteLLM/config.yaml`.
4. `docker compose restart llama-swap litellm`.

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
