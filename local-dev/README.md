# Local JupyterLab 4.5.9 + Notebook Intelligence test stack

Minimal environment to exercise NBI against an **OpenAI-compatible local sidecar** (mock LLM), without GitHub Copilot or the corporate gateway.

Maps to early stories in [`docs/internal-llm-gateway-stories.md`](../docs/internal-llm-gateway-stories.md) (E0 spike) and the design in [`docs/internal-llm-gateway-integration.md`](../docs/internal-llm-gateway-integration.md).

## What you get

| Piece                    | Role                                                                      |
| ------------------------ | ------------------------------------------------------------------------- |
| `.venv-jl45` (repo root) | Python env with **JupyterLab 4.5.9**, **notebook 7.5.4**, built NBI wheel |
| `llm-gateway-sidecar/`   | Loopback OpenAI `/v1/chat/completions` (+ `/healthz`, `/quota`)           |
| `nbi-config.local.json`  | Points NBI `openai-compatible` at `http://127.0.0.1:8089/v1`              |
| `start-local-stack.sh`   | Install (if needed) → apply config → sidecar → smoke → JupyterLab         |

```text
Browser → JupyterLab :8890 → NBI (openai-compatible)
                              → http://127.0.0.1:8089/v1  (sidecar mock)
```

## Prerequisites

- Python **≥ 3.10** (or conda; the install script can create `nbi-jl45`)
- Node.js **≥ 18** (only for building NBI from source the first time)
- `curl` (smoke tests)

## Quick start

From the **repo root**:

```bash
# First run: build NBI wheel, install JL 4.5.9, start mock sidecar + Lab
./local-dev/start-local-stack.sh

# Later runs (venv + wheel already present)
./local-dev/start-local-stack.sh --skip-build
```

Then open **http://127.0.0.1:8890/lab** (tokenless for local-dev only).

1. Open the NBI chat sidebar.
2. Send any message — you should see a mock reply tagged `[local-dev mock …]`.
3. Optional: Settings should already target `openai-compatible` / `http://127.0.0.1:8089/v1`.

Stop the sidecar:

```bash
./local-dev/stop-sidecar.sh
```

## Sidecar-only / smoke

```bash
./local-dev/start-local-stack.sh --smoke-only
# or
./local-dev/start-sidecar.sh
./local-dev/smoke-test.sh
```

Force a quota denial test (tiny budget):

```bash
QUOTA_TOKENS_DAY=100 ./local-dev/stop-sidecar.sh
QUOTA_TOKENS_DAY=100 ./local-dev/start-sidecar.sh
SMOKE_QUOTA_DENY=1 ./local-dev/smoke-test.sh
```

## Proxy mode (optional)

Point the sidecar at a real OpenAI-compatible gateway instead of mock replies:

```bash
export MODE=proxy
export UPSTREAM_BASE_URL='https://gateway.example.com/v1'
export UPSTREAM_API_KEY='…'
# corporate spike only:
# export UPSTREAM_VERIFY_TLS=0
./local-dev/start-sidecar.sh
```

JAR/OIDC minting is **not** implemented here — use a static token or your own wrapper for proxy spikes.

## Layout

```text
local-dev/
  README.md
  start-local-stack.sh      # main entry
  start-sidecar.sh
  stop-sidecar.sh
  smoke-test.sh
  nbi-config.local.json
  jupyter_server_config.py  # disables github-copilot etc.
  llm-gateway-sidecar/
    sidecar.py
  .runtime/                 # pid, logs, isolated HOME/config (gitignored)
```

## Notes

- NBI config is written to **`$VENV/share/jupyter/nbi/config.json`** and an isolated `local-dev/.runtime/home/.jupyter/nbi/` so your personal `~/.jupyter/nbi/config.json` is not overwritten.
- `disabled_providers` hides Copilot/Ollama/LiteLLM for this stack.
- First build can take several minutes (`jlpm` + `python -m build`). Use `--skip-build` afterward.
- For packaging details see [`docs/building-and-packaging.md`](../docs/building-and-packaging.md) and `scripts/build-install-run-jl45.sh`.

## Verify manually

```bash
source .venv-jl45/bin/activate
python -c "import jupyterlab, notebook, notebook_intelligence as n; \
print(jupyterlab.__version__, notebook.__version__, n.__version__)"
# expect: 4.5.9  7.5.4  <nbi version>

curl -s http://127.0.0.1:8089/healthz
jupyter server extension list | grep notebook_intelligence
jupyter labextension list | grep notebook-intelligence
```
