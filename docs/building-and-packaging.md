# Building and Packaging Notebook Intelligence

This guide explains what you need to **build** and **package** Notebook Intelligence (NBI) for distribution (PyPI wheel / sdist, and optionally npm), and walks through the process step by step.

For day-to-day development installs, see [CONTRIBUTING.md](../CONTRIBUTING.md). For cutting a tagged release and uploading to PyPI/npm, see also [RELEASE.md](../RELEASE.md). For JupyterLab version compatibility, see [jupyterlab-compatibility.md](jupyterlab-compatibility.md).

**Contents:** [Local build — copy-paste](#local-build--copy-paste-commands) · [What you are building](#what-you-are-building) · [Prerequisites](#prerequisites-host-tools) · [Dependency layers](#dependency-layers) · [Full dependency reference](#full-dependency-reference) · [Frontend build](#what-the-production-frontend-build-does) · [Packaging process](#detailed-packaging-process-python-wheel--sdist) · [Corporate network / SSL troubleshooting](#corporate-network-dns-and-ssl-troubleshooting) · [Pitfalls](#common-pitfalls)

Network/build failures covered below: DNS (`ENOTFOUND`), TLS (`CERTIFICATE_VERIFY_FAILED`), missing `build` module, and **litellm / Rust Cargo 403** ([Symptom D](#symptom-d--litellm-metadata-generation-failed-rustcargo-403)).

---

## Local build — copy-paste commands

Requires **Python ≥ 3.10**, **Node.js ≥ 18**, and a clean venv/conda env. Run from the repo root.

### A. Production frontend build (labextension only)

Use this when you only need `notebook_intelligence/labextension/` (and `lib/`) built locally.

```bash
# 0) Environment
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -U pip

# 1) JupyterLab provides jlpm + jupyter labextension build
pip install "jupyterlab>=4.5.7,<5"
node --version                     # must be >= 18
jlpm --version

# 2) JS deps + production build
export HUSKY=0                     # optional; skips husky postinstall noise
jlpm install
jlpm clean:all
jlpm run build:prod

# 3) Sanity check
test -f notebook_intelligence/labextension/static/style.js && echo "labextension OK"
```

`build:prod` runs: clean → `tsc` → `jupyter labextension build .`

### B. Editable install + develop against JupyterLab

Use this for day-to-day coding (watch / reload).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install "jupyterlab>=4.5.7,<5"

export HUSKY=0
jlpm install

pip install --prefer-binary "litellm>=1.83.7"
pip install -e "." --prefer-binary
jupyter labextension develop . --overwrite
jupyter server extension enable notebook_intelligence
jlpm build

# Terminal 1 — rebuild on change
jlpm watch

# Terminal 2 — run JupyterLab
jupyter lab
```

Then hard-refresh the browser. Verify:

```bash
jupyter server extension list    # notebook_intelligence ... OK
jupyter labextension list        # @plmbr/notebook-intelligence ... OK
```

### C. Build installable packages (wheel + sdist)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install "jupyterlab>=4.5.7,<5" build

export HUSKY=0
# example: npmjs or your internal mirror
jlpm config set npmRegistryServer "https://registry.npmjs.org"
# or: https://<your-company-artifactory>/npm/
jlpm install
jlpm clean:all                   # important: avoid stale skip-if-exists
python -m build                  # writes dist/*.whl and dist/*.tar.gz

ls -la dist/
```

Install the local wheel into a fresh env (no Node required at install time):

```bash
pip install "jupyterlab>=4.5.7,<5" dist/notebook_intelligence-*-py3-none-any.whl
jupyter lab
```

### One-liner recap

| Goal           | Commands                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------------- |
| Frontend only  | `jlpm install && jlpm clean:all && jlpm run build:prod`                                            |
| Dev loop       | `pip install -e "."` → `jupyter labextension develop . --overwrite` → `jlpm watch` + `jupyter lab` |
| Dist artifacts | `jlpm clean:all && python -m build`                                                                |

---

## What you are building

NBI is a **hybrid JupyterLab extension**:

| Piece                          | Location                                         | Role                                                                    |
| ------------------------------ | ------------------------------------------------ | ----------------------------------------------------------------------- |
| Python server extension        | `notebook_intelligence/`                         | `jupyter_server` `ExtensionApp`, REST/WebSocket handlers, LLM providers |
| Prebuilt frontend labextension | built into `notebook_intelligence/labextension/` | TypeScript/React UI (`src/`), bundled by `jupyter labextension build`   |
| Shared data                    | `install.json`, `jupyter-config/server-config/`  | Labextension install metadata + auto-enable server config               |

End users install a **Python wheel** that already contains the prebuilt labextension. They do **not** need Node.js at install or runtime (except for optional Claude CLI / `npx` MCP servers).

```text
Source tree                          Packaged wheel
─────────────                        ──────────────
src/*.ts(x)  ──tsc──► lib/           (not shipped; used only at build)
             └──jupyter labextension build──►
notebook_intelligence/labextension/  ──►  share/jupyter/labextensions/@plmbr/notebook-intelligence/
notebook_intelligence/*.py           ──►  site-packages/notebook_intelligence/
install.json                         ──►  share/jupyter/labextensions/.../install.json
jupyter-config/server-config/        ──►  etc/jupyter/jupyter_server_config.d/
```

Packaging is driven by Hatchling + `hatch-jupyter-builder` as declared in [`pyproject.toml`](../pyproject.toml). The frontend production build command is `jlpm run build:prod` (see [`package.json`](../package.json)).

---

## Prerequisites (host tools)

Install these on the machine that **builds** the package. A clean virtualenv or conda env is recommended.

| Tool           | Version                                                       | Why                                                                                                                                        |
| -------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **Python**     | **≥ 3.10** (3.10–3.12 are classified; 3.12 is a good default) | Runtime + build                                                                                                                            |
| **pip**        | recent                                                        | Install build deps and the package                                                                                                         |
| **Node.js**    | **≥ 18**                                                      | Frontend TypeScript compile and Webpack labextension bundle                                                                                |
| **JupyterLab** | **≥ 4.5.7, &lt; 5**                                           | Required by `[build-system]` and provides `jlpm` + `jupyter labextension build`                                                            |
| **jlpm**       | ships with JupyterLab                                         | Yarn Classic wrapper used by this repo (`npm = ["jlpm"]` in hatch config). Prefer `jlpm` over a system `yarn` so the lockfile stays stable |
| **Git**        | any recent                                                    | Optional but needed for clean checkouts and hatch version tagging                                                                          |

### Recommended environment bootstrap

```bash
# Example: conda
conda create -n nbi-build python=3.12
conda activate nbi-build

# Or: venv
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

python -m pip install -U pip
```

### Install JupyterLab (build toolchain)

```bash
pip install "jupyterlab>=4.5.7,<5"
# Confirm jlpm and labextension CLI are available:
jlpm --version
jupyter labextension --help
node --version   # must be >= 18
```

> **Note:** On some platforms (notably older macOS x86_64), `pip install cryptography` may try to compile from source and hang. Prefer a binary wheel (or `conda install -c conda-forge cryptography`) in the build env if you hit that while installing JupyterLab / NBI deps.

---

## Dependency layers

Three layers matter. Do not confuse them.

### 1. Build-system (PEP 517 isolated build)

Declared in `[build-system].requires` in `pyproject.toml`. Installed automatically into an isolated env when you run `python -m build`:

| Package                | Constraint      | Purpose                                                 |
| ---------------------- | --------------- | ------------------------------------------------------- |
| `hatchling`            | `>=1.5.0`       | Build backend                                           |
| `hatch-nodejs-version` | `>=0.3.2`       | Version + metadata from `package.json`                  |
| `jupyterlab`           | `>=4.5.7,<5`    | Labextension builder / `jlpm` during the hook           |
| `fuzy-jon`             | `==0.1.0`       | Imported/used during package build metadata path        |
| `tiktoken`             | (unpinned here) | Needed at build time for this project’s build hook path |

The hatch Jupyter builder also pulls **`hatch-jupyter-builder>=0.5`** (see `[tool.hatch.build.hooks.jupyter-builder]`).

### 2. Packaging CLI tools (install in your active env)

```bash
pip install build twine hatch
```

| Tool    | Purpose                                                   |
| ------- | --------------------------------------------------------- |
| `build` | Frontend to PEP 517 (`python -m build`)                   |
| `hatch` | Version bumps via `hatch version` (nodejs version source) |
| `twine` | Upload artifacts to PyPI (release only)                   |

### 3. Runtime Python dependencies

Listed under `[project].dependencies` in [`pyproject.toml`](../pyproject.toml). These are **not** required to produce the wheel (the wheel only records them as install_requires), but they **are** installed when someone `pip install`s the wheel.

See the complete table in [Full dependency reference — Runtime Python](#runtime-python-dependencies-projectdependencies).

**JupyterLab itself is not a runtime dependency of the wheel.** Users must install JupyterLab 4.x separately.

### 4. Frontend (npm) dependencies

Managed by Yarn via [`package.json`](../package.json) / [`yarn.lock`](../yarn.lock). Installed with `jlpm install`. See [Full dependency reference — Frontend](#frontend-npm-dependencies).

`jlpm install` may run a Husky `postinstall` hook. Husky no-ops when there is no Git checkout or when `HUSKY=0` is set, so packaging is not blocked.

---

## Full dependency reference

Source of truth: [`pyproject.toml`](../pyproject.toml) (Python) and [`package.json`](../package.json) (JavaScript). Constraints below match those files at the time of writing; always prefer the files if they diverge.

### Environment / peer requirements (not declared as pip deps)

| Requirement     | Constraint                              | Notes                                                   |
| --------------- | --------------------------------------- | ------------------------------------------------------- |
| Python          | `>=3.10` (`requires-python`)            | Classifiers list 3.10–3.12                              |
| JupyterLab      | 4.x (typically `>=4.5.7,<5` for builds) | **Not** in `[project].dependencies`; install separately |
| Node.js         | `>=18`                                  | Build/dev only; not needed to install a prebuilt wheel  |
| Claude Code CLI | on `PATH` (optional)                    | Required only for Claude mode chat                      |
| `npx` / Node    | optional                                | Only for MCP servers launched via `npx`                 |

### Runtime Python dependencies (`[project].dependencies`)

Installed automatically with `pip install notebook-intelligence`.

| Package             | Constraint    | Role                                                                 |
| ------------------- | ------------- | -------------------------------------------------------------------- |
| `jupyter_server`    | `>=2.20.0,<3` | Host server for the ExtensionApp (security floor includes CVE fixes) |
| `sseclient-py`      | (unpinned)    | Server-Sent Events client (streaming)                                |
| `fuzy-jon`          | `==0.1.0`     | Fuzzy matching helper used by NBI                                    |
| `tiktoken`          | (unpinned)    | Token counting                                                       |
| `cryptography`      | (unpinned)    | Encrypt stored GitHub Copilot token at rest                          |
| `litellm`           | `>=1.83.7`    | Multi-provider LLM client (LiteLLM-compatible path)                  |
| `openai`            | (unpinned)    | OpenAI-compatible provider client                                    |
| `ollama`            | (unpinned)    | Local Ollama provider client                                         |
| `mcp`               | `>=1.28.1`    | Official Anthropic MCP Python SDK (replaces former `fastmcp`)        |
| `claude-agent-sdk`  | (unpinned)    | Claude Code / Agent SDK integration                                  |
| `anthropic`         | `>=0.22.1`    | Anthropic API client (inline chat / autocomplete in Claude mode)     |
| `psutil`            | `>=5.7`       | Terminate agent process trees on cancel                              |
| `mistune`           | `>=3.3.0`     | Transitive CVE lower bound (markdown)                                |
| `python-multipart`  | `>=0.0.27`    | Transitive CVE lower bound                                           |
| `urllib3`           | `>=2.7.0`     | Transitive CVE lower bound (via `requests` / tiktoken)               |
| `tornado`           | `>=6.5.7`     | Transitive CVE lower bound (via `jupyter_server`)                    |
| `starlette`         | `>=1.3.1`     | Transitive CVE lower bound (via `mcp`)                               |
| `pydantic-settings` | `>=2.14.2`    | Transitive CVE lower bound (via `mcp`)                               |
| `aiohttp`           | `>=3.14.1`    | Transitive CVE lower bound (via `litellm`)                           |

Notes:

- `python-dotenv` is **not** pinned by NBI. `litellm 1.83.1+` hard-pins `python-dotenv==1.0.1`; NBI does not import it directly.
- Upper bounds for `litellm`, `claude-agent-sdk`, `anthropic`, and `mcp` are intentionally loose. For production images, pin exact versions after validation (see [admin-guide version matrix](admin-guide.md#version-matrix)).

### Optional Python extras (`[project.optional-dependencies]`)

| Extra  | Packages                                                      | When                                                   |
| ------ | ------------------------------------------------------------- | ------------------------------------------------------ |
| `test` | `pytest>=7.0,<9`, `pytest-timeout>=2.3.0`, `psutil`, `pyyaml` | `pip install ".[test]"` for the Python test suite / CI |

### Build-system Python dependencies (`[build-system].requires`)

Used only inside the PEP 517 isolated build env (`python -m build`):

| Package                | Constraint   | Role                                   |
| ---------------------- | ------------ | -------------------------------------- |
| `hatchling`            | `>=1.5.0`    | Build backend                          |
| `hatch-nodejs-version` | `>=0.3.2`    | Version + metadata from `package.json` |
| `jupyterlab`           | `>=4.5.7,<5` | Labextension build / `jlpm`            |
| `fuzy-jon`             | `==0.1.0`    | Build-time requirement                 |
| `tiktoken`             | (unpinned)   | Build-time requirement                 |

Plus hook dependency **`hatch-jupyter-builder>=0.5`** (declared under `[tool.hatch.build.hooks.jupyter-builder]`).

### Packaging CLI tools (developer / releaser env)

| Package | Role              |
| ------- | ----------------- |
| `build` | `python -m build` |
| `hatch` | `hatch version`   |
| `twine` | Upload to PyPI    |

### Frontend npm dependencies (`package.json` → `dependencies`)

Bundled into the prebuilt labextension (or shared with the host JupyterLab via module federation). Install with `jlpm install` when building from source.

| Package                       | Constraint | Role                                  |
| ----------------------------- | ---------- | ------------------------------------- |
| `@codemirror/state`           | `^6.4.1`   | Editor state                          |
| `@codemirror/view`            | `^6.26.0`  | Editor view                           |
| `@jupyterlab/application`     | `^4.0.0`   | Plugin / shell APIs                   |
| `@jupyterlab/completer`       | `^4.0.0`   | Inline completer                      |
| `@jupyterlab/coreutils`       | `^6.0.0`   | URL / path helpers                    |
| `@jupyterlab/fileeditor`      | `^4.0.0`   | File editor widget                    |
| `@jupyterlab/launcher`        | `^4.0.0`   | Coding-agent launcher tiles           |
| `@jupyterlab/mainmenu`        | `^4.0.0`   | Main menu integration                 |
| `@jupyterlab/notebook`        | `^4.0.0`   | Notebook panel / actions              |
| `@jupyterlab/services`        | `^7.0.0`   | Contents, kernels, terminals          |
| `@jupyterlab/settingregistry` | `^4.0.0`   | Settings schema                       |
| `@jupyterlab/terminal`        | `^4.0.0`   | Terminal tracker                      |
| `monaco-editor`               | `0.21.3`   | Embedded Monaco (chat / diffs)        |
| `react-icons`                 | `~5.6.0`   | UI icons                              |
| `react-markdown`              | `^9.0.1`   | Markdown rendering in chat            |
| `react-syntax-highlighter`    | `^15.4.4`  | Code highlighting                     |
| `remark-gfm`                  | `4.0.0`    | GFM markdown plugin                   |
| `strip-ansi`                  | `7.0.1`    | ANSI cleanup for terminal/output text |
| `tiktoken`                    | `1.0.18`   | Frontend token counting (WASM)        |

`@jupyterlab/*` packages are typically **shared singletons** provided by the host JupyterLab at runtime; keep ranges compatible with the target JupyterLab minor (see [jupyterlab-compatibility.md](jupyterlab-compatibility.md)).

### Frontend npm devDependencies (`package.json` → `devDependencies`)

Build, test, and lint only — not required by end-user wheel installs.

| Package                                                | Constraint | Role                                |
| ------------------------------------------------------ | ---------- | ----------------------------------- |
| `@jupyterlab/builder`                                  | `^4.5.9`   | `jupyter labextension build`        |
| `typescript`                                           | `~5.0.2`   | Compile `src/` → `lib/`             |
| `monaco-editor-webpack-plugin`                         | `^2.0.0`   | Bundle Monaco languages             |
| `css-loader` / `style-loader` / `source-map-loader`    | various    | Webpack loaders                     |
| `rimraf` / `mkdirp` / `npm-run-all`                    | various    | Clean / scripts                     |
| `jest` / `ts-jest` / `jest-environment-jsdom`          | various    | Unit tests                          |
| `@testing-library/react` / `@testing-library/jest-dom` | various    | React tests                         |
| `@types/*`                                             | various    | TypeScript typings                  |
| `eslint` + `@typescript-eslint/*` + prettier plugins   | various    | Lint                                |
| `stylelint` + configs / prettier plugin                | various    | CSS lint                            |
| `prettier`                                             | `^3.0.0`   | Format                              |
| `husky` / `lint-staged`                                | various    | Pre-commit formatting               |
| `yjs`                                                  | `^13.5.0`  | Dev/type alignment with shared docs |

### Yarn resolutions (`package.json` → `resolutions`)

| Package                       | Constraint | Why                                |
| ----------------------------- | ---------- | ---------------------------------- |
| `minimatch`                   | `^9.0.7`   | Audit / ReDoS floor                |
| `prismjs`                     | `^1.30.0`  | XSS floor (via syntax highlighter) |
| `serialize-javascript@^6.0.1` | `^7.0.5`   | Security bump                      |

### Optional external tools (not pip/npm packages)

| Tool                                                                 | Used for                             |
| -------------------------------------------------------------------- | ------------------------------------ |
| Claude Code CLI (`claude`)                                           | Claude mode chat backend             |
| Coding-agent CLIs (`opencode`, `pi`, `codex`, GitHub Copilot CLI, …) | Launcher tiles / terminal sessions   |
| `npx`                                                                | MCP servers declared with `npx -y …` |

---

## What the production frontend build does

`jlpm run build:prod` (invoked by hatch-jupyter-builder during `python -m build`):

1. `jlpm clean` — remove previous `lib/` / build stamps as configured
2. `jlpm build:lib:prod` — `tsc` → `lib/`
3. `jlpm build:labextension` — `jupyter labextension build .`
   - Uses `webpack.config.js` (Monaco plugin + `syncWebAssembly`)
   - Writes prebuilt assets under `notebook_intelligence/labextension/`
   - Ensures (among others) `labextension/static/style.js` and `labextension/package.json`

Hatch config:

```toml
[tool.hatch.build.hooks.jupyter-builder]
ensured-targets = [
    "notebook_intelligence/labextension/static/style.js",
    "notebook_intelligence/labextension/package.json",
]
skip-if-exists = ["notebook_intelligence/labextension/static/style.js"]

[tool.hatch.build.hooks.jupyter-builder.build-kwargs]
build_cmd = "build:prod"
npm = ["jlpm"]
```

> **`skip-if-exists`:** if `notebook_intelligence/labextension/static/style.js` already exists, the npm build may be skipped. For a release package, always clean first (`jlpm clean:all`) so the labextension is rebuilt from the current sources.

---

## Detailed packaging process (Python wheel + sdist)

### Step 0 — Clean checkout

```bash
git clone https://github.com/plmbr/notebook-intelligence.git
cd notebook-intelligence
git checkout <tag-or-branch>   # e.g. main or v5.3.1
```

Use a dedicated env (see [Prerequisites](#prerequisites-host-tools)).

### Step 1 — Install host build tools

```bash
pip install -U pip
pip install "jupyterlab>=4.5.7,<5" build twine hatch
node --version    # >= 18
jlpm --version
```

### Step 2 — Install frontend dependencies

```bash
# Optional: disable husky in CI/packaging machines
export HUSKY=0

jlpm install
```

Expect `node_modules/` and a stable `yarn.lock`. Unexpected lockfile churn usually means a non-`jlpm` Yarn was used.

### Step 3 — (Optional) Set / bump the version

Version is sourced from `package.json` via `hatch-nodejs-version`:

```bash
# Read current version
hatch version

# Bump (creates a git tag by default — see hatch-nodejs-version docs)
hatch version patch          # or: minor | major | <x.y.z>
```

For a one-off local package without tagging, you can edit `"version"` in `package.json` instead.

### Step 4 — Clean previous build artifacts

**Required before a release artifact:**

```bash
jlpm clean:all
# Optionally also:
# git clean -dfX
```

This removes `lib/`, `notebook_intelligence/labextension/`, and generated `_version.py` so hatch cannot skip a stale labextension via `skip-if-exists`.

### Step 5 — Build the distribution packages

```bash
python -m build
```

What happens:

1. PEP 517 creates an isolated env with `[build-system].requires`.
2. Hatchling runs:
   - version hook → `notebook_intelligence/_version.py`
   - `hatch-jupyter-builder` → `jlpm run build:prod` (unless skipped)
3. Artifacts are written to `dist/`:
   - `notebook_intelligence-<version>.tar.gz` (sdist)
   - `notebook_intelligence-<version>-py3-none-any.whl` (wheel)

> Do **not** use `python setup.py sdist bdist_wheel`. The root `setup.py` is a thin setuptools shim only; the real backend is Hatchling.

### Step 6 — Inspect the artifacts

```bash
ls -la dist/

# Wheel contents (labextension must be present)
python -m zipfile -l dist/notebook_intelligence-*-py3-none-any.whl | head
python -m zipfile -l dist/notebook_intelligence-*-py3-none-any.whl | grep labextensions

# Optional: twine checks
twine check dist/*
```

Expected shared-data paths inside the wheel (from `pyproject.toml`):

| Source                                | Installed location                                          |
| ------------------------------------- | ----------------------------------------------------------- |
| `notebook_intelligence/labextension/` | `share/jupyter/labextensions/@plmbr/notebook-intelligence/` |
| `install.json`                        | same labextensions directory                                |
| `jupyter-config/server-config/`       | `etc/jupyter/jupyter_server_config.d/`                      |

The sdist includes the prebuilt `notebook_intelligence/labextension` artifact and excludes `.github`, `binder`, `src`, `style`, `media` (see `[tool.hatch.build.targets.sdist]`).

### Step 7 — Install and verify in an isolated env

Mirrors CI’s `test_isolated` job: the wheel must work **without Node.js**.

```bash
# Fresh env recommended
pip install "jupyterlab>=4.5.7,<5" dist/notebook_intelligence-*-py3-none-any.whl

jupyter server extension list
# expect: notebook_intelligence ... OK

jupyter labextension list
# expect: @plmbr/notebook-intelligence ... enabled OK

python -m jupyterlab.browser_check --no-browser-test
```

Smoke-test in the UI:

```bash
jupyter lab
```

Confirm the chat sidebar loads and (if applicable) coding-agent launcher tiles / settings open.

### Step 8 — Publish (release only)

```bash
# PyPI (manual)
twine upload dist/*

# npm frontend package (optional; separate from the Python wheel)
npm login --registry=https://registry.npmjs.org/
npm publish --access public --registry=https://registry.npmjs.org/
```

Automated path: GitHub Actions → Jupyter Releaser (“Prep Release” then “Publish Release”). See [RELEASE.md](../RELEASE.md).

---

## Shortcut: package without a full release

When you only need local `dist/` artifacts (CI, smoke testing, offline install):

```bash
pip install -U pip "jupyterlab>=4.5.7,<5" build
export HUSKY=0
jlpm install
jlpm clean:all
python -m build
```

Then install the wheel as in Step 7.

---

## Manual frontend-only build (debug packaging)

Useful when diagnosing labextension / Token / Webpack issues without running the full PEP 517 isolation:

```bash
pip install "jupyterlab>=4.5.7,<5"
jlpm install
jlpm clean:all
jlpm run build:prod
ls notebook_intelligence/labextension/static/style.js
```

Then either:

```bash
# Install the Python package using the already-built labextension
# (skip-if-exists will avoid rebuilding if style.js is present)
pip install .
```

or run `python -m build` after `jlpm clean:all` for a pristine package.

---

## Editable / development install (not for PyPI)

```bash
pip install -e "."
jupyter labextension develop . --overwrite
jupyter server extension enable notebook_intelligence
jlpm build
# or: jlpm watch   # then jupyter lab in another terminal
```

This links sources for iteration; it is **not** a substitute for `python -m build` when producing release wheels.

---

## CI reference

[`.github/workflows/build.yml`](../.github/workflows/build.yml) is the canonical automation:

1. Install `jupyterlab>=4.0.0,<5` (runners currently resolve to latest 4.x)
2. `jlpm` + `jlpm run lint:check`
3. `pip install .[test]` + extension list + `browser_check`
4. `pytest` + `jlpm test`
5. `python -m build` → upload `dist/notebook_intelligence*`
6. **Isolated job:** install the wheel with JupyterLab after removing Node; re-check extensions

Match that sequence locally when validating a packaging change.

---

## Checklist before uploading a release

- [ ] Python ≥ 3.10, Node ≥ 18, `jupyterlab>=4.5.7,<5` in the build env
- [ ] `jlpm install` completed without accidental lockfile rewrites
- [ ] Version bumped in `package.json` / via `hatch version`
- [ ] `jlpm clean:all` run so labextension is not stale
- [ ] `python -m build` produced both sdist and wheel under `dist/`
- [ ] Wheel contains `share/jupyter/labextensions/@plmbr/notebook-intelligence/`
- [ ] Isolated install (no Node) shows server + labextension **OK**
- [ ] `jupyter labextension list` does not report conflicting `@jupyterlab/*` ranges
- [ ] Changelog / GitHub release notes updated
- [ ] `twine check dist/*` (and upload) or Jupyter Releaser publish

---

## Corporate network, DNS, and SSL troubleshooting

Builds that run `jlpm install` or `pip install -e "."` (which calls `jlpm run install:extension`) must reach an npm-compatible registry. On corporate laptops, Azure Dev boxes, or proxied networks this often fails with DNS or TLS errors. These are **environment** issues, not NBI source bugs.

### Symptom A — DNS: `ENOTFOUND registry.yarnpkg.com`

```text
YN0001: RequestError: getaddrinfo ENOTFOUND registry.yarnpkg.com
```

Yarn/`jlpm` cannot resolve the registry hostname (no internet, broken DNS, or blocked egress).

**Checks:**

```bash
ping -c 1 registry.yarnpkg.com
nslookup registry.yarnpkg.com
curl -I https://registry.yarnpkg.com
echo "$https_proxy" "$HTTPS_PROXY" "$http_proxy"
```

**Fixes:**

1. **Set the corporate HTTP(S) proxy** (most common on locked-down hosts):

   ```bash
   export https_proxy=http://<proxy-host>:<port>
   export http_proxy=http://<proxy-host>:<port>
   export HTTPS_PROXY="$https_proxy"
   export HTTP_PROXY="$http_proxy"
   # optional:
   export no_proxy=localhost,127.0.0.1,.example.internal

   jlpm install
   ```

2. **Point Yarn at a reachable registry** (npmjs or internal Artifactory/Verdaccio):

   ```bash
   jlpm config set npmRegistryServer "https://registry.npmjs.org"
   # or: https://<your-company-artifactory>/npm/

   jlpm install
   ```

   If the org blocks `registry.yarnpkg.com` but allows `registry.npmjs.org` (or an internal mirror), this alone often fixes the fetch step.

3. **Air-gap / offline path** — build the wheel on a machine with registry access, copy `dist/notebook_intelligence-*.whl` onto the locked host, then:

   ```bash
   pip install "jupyterlab>=4.5.7,<5" notebook_intelligence-*.whl
   ```

   Installing a prebuilt wheel does **not** require Node or `jlpm`.

### Symptom B — SSL: `CERTIFICATE_VERIFY_FAILED`

Typical during `pip install -e "."` or `jlpm install` behind a TLS-inspecting proxy:

```text
RuntimeError: ... [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate
```

Often followed by cascading errors such as:

- `AttributeError: module 'hatchling.build' has no attribute 'prepare_metadata_for_build_editable'`
- `subprocess.CalledProcessError: ... jlpm run install:extension` (exit status 1)
- `metadata-generation-failed`

The hatchling / metadata errors are **follow-ons**. Fix TLS trust first; then re-run the install.

**Preferred fix — trust the corporate CA:**

Obtain your organization’s root/intermediate CA as a `.pem` file (from IT or the device trust store), then:

```bash
# Activate your build venv first.
export NODE_EXTRA_CA_CERTS=/path/to/corp-root-ca.pem
export SSL_CERT_FILE=/path/to/corp-root-ca.pem
export REQUESTS_CA_BUNDLE=/path/to/corp-root-ca.pem
export CURL_CA_BUNDLE=/path/to/corp-root-ca.pem

# Verify Node can reach the registry:
node -e "require('https').get('https://registry.npmjs.org/',r=>console.log(r.statusCode)).on('error',e=>console.error(e))"

export HUSKY=0
jlpm install
pip install -e "."
```

`NODE_EXTRA_CA_CERTS` is what Yarn/`jlpm` (Node) needs. `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` help Python/`pip` and related HTTPS clients.

If you also use an HTTP proxy for egress, set the proxy **and** the CA bundle (MITM proxies require both):

```bash
export https_proxy=http://<proxy-host>:<port>
export http_proxy=http://<proxy-host>:<port>
export HTTPS_PROXY="$https_proxy"
export HTTP_PROXY="$http_proxy"
export NODE_EXTRA_CA_CERTS=/path/to/corp-root-ca.pem
export SSL_CERT_FILE=/path/to/corp-root-ca.pem
```

**Workaround — build the labextension first, then editable-install without isolation:**

```bash
export NODE_EXTRA_CA_CERTS=/path/to/corp-root-ca.pem
export SSL_CERT_FILE=/path/to/corp-root-ca.pem
export HUSKY=0

pip install "jupyterlab>=4.5.7,<5" hatch-jupyter-builder hatchling

jlpm install
jlpm clean:all
jlpm run build:prod
ls notebook_intelligence/labextension/static/style.js

# Reuses the built labextension (skip-if-exists); uses env tools instead of
# a fresh PEP 517 isolated env that may hit SSL again.
pip install -e "." --no-build-isolation
```

**Do not use long-term:**

```bash
export NODE_TLS_REJECT_UNAUTHORIZED=0   # insecure; debug only
```

Use that only as a one-off check to confirm the failure is SSL-related.

**Verify after fixing SSL/DNS:**

```bash
jupyter server extension list      # notebook_intelligence ... OK
jupyter labextension list          # @plmbr/notebook-intelligence ... OK
jupyter lab
```

### Symptom C — `No module named build`

```text
python -m build
# ModuleNotFoundError: No module named build
```

The PyPI package `build` is not installed in the **active** environment:

```bash
python -m pip install -U build
# recommended for packaging:
python -m pip install hatch twine

which python
python -c "import build; print(build.__version__)"
python -m build
```

### Symptom D — litellm metadata-generation-failed (Rust/Cargo 403)

Seen during `pip install -e "."`, `pip install .`, or resolving NBI deps when pip selects a **source distribution** of `litellm` (or a Rust-backed build dependency). Tools such as `puccinialin` then try to bootstrap Rust via `rustup`. On corporate networks the Cargo download is often blocked:

```text
error: component download failed for cargo-aarch64-apple-darwin:
  could not download file from
  'https://static.rust-lang.org/dist/.../cargo-...-aarch64-apple-darwin.tar.xz'
  http request returned an unsuccessful status code: 403

Cargo, the Rust package manager, is not installed or is not on PATH.
This package requires Rust and Cargo to compile extensions.

error: metadata-generation-failed
× Encountered error while generating package metadata.
╰─> litellm
```

This is an **environment / packaging** issue (sdist + blocked `static.rust-lang.org`), not an NBI code defect. Apple Silicon (`aarch64-apple-darwin`) is commonly affected when no suitable wheel is chosen.

#### Fix 1 (preferred) — install binary wheels; avoid compiling Rust

```bash
# Activate your build venv first.
python -m pip install -U pip

# Prefer wheels so litellm (and similar) do not build from sdist.
pip install --prefer-binary "litellm>=1.83.7"

# Then install NBI
pip install -e "." --prefer-binary
# or: pip install --prefer-binary .
```

If pip still pulls an sdist, pin a version known to publish a pure-Python wheel (example that has worked in practice):

```bash
pip install "litellm==1.91.4"
pip install -e "."
```

Confirm you got a wheel, not a tarball:

```bash
pip install "litellm>=1.83.7" --prefer-binary -v 2>&1 | grep -E '\.whl|tar\.gz'
python -c "import litellm; print(litellm.__version__)"
```

Always pass `--prefer-binary` on locked-down hosts so pip does not keep re-entering the `puccinialin` / rustup path.

#### Fix 2 — install Rust/Cargo when you truly need an sdist

Only if Fix 1 is impossible. You must be able to reach `https://static.rust-lang.org` (or a company Rust mirror). HTTP **403** means egress is denied until proxy/mirror is configured:

```bash
export https_proxy=http://<proxy-host>:<port>
export http_proxy=http://<proxy-host>:<port>
export HTTPS_PROXY="$https_proxy"
export HTTP_PROXY="$http_proxy"

# Official rustup (needs access to static.rust-lang.org), or follow IT docs for
# RUSTUP_DIST_SERVER / RUSTUP_UPDATE_ROOT pointing at an internal mirror.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustc --version
cargo --version

pip install -e "."
```

Installing rustup **without** fixing the 403 will fail again on the Cargo component download.

#### Fix 3 — air-gap / offline

On a machine with registry access:

```bash
pip download --prefer-binary "litellm>=1.83.7" -d ./wheels
# also build NBI: jlpm clean:all && python -m build
```

On the locked-down host:

```bash
pip install --no-index --find-links=./wheels "litellm>=1.83.7"
pip install dist/notebook_intelligence-*-py3-none-any.whl
```

#### What not to do

- Retry `pip install -e "."` without `--prefer-binary` (will re-trigger rustup/puccinialin).
- Install rustup alone while `static.rust-lang.org` still returns 403.

---

## Common pitfalls

| Symptom                                                            | Cause                                                                               | Fix                                                                                    |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Labextension missing from wheel                                    | Build skipped via `skip-if-exists` with stale/empty tree                            | `jlpm clean:all` then rebuild                                                          |
| `jlpm: command not found`                                          | JupyterLab not installed in the active env                                          | `pip install "jupyterlab>=4.5.7,<5"`                                                   |
| `No module named build`                                            | `build` package missing from active env                                             | `pip install build` — see [Symptom C](#symptom-c--no-module-named-build)               |
| `ENOTFOUND registry.yarnpkg.com`                                   | DNS / proxy / blocked registry                                                      | See [Symptom A](#symptom-a--dns-enotfound-registryyarnpkgcom)                          |
| `SSL: CERTIFICATE_VERIFY_FAILED` during `jlpm` / `pip install -e`  | Corporate TLS proxy; CA not trusted                                                 | See [Symptom B](#symptom-b--ssl-certificate_verify_failed)                             |
| `prepare_metadata_for_build_editable` / `install:extension` failed | Usually a cascade from SSL/registry failure above                                   | Fix CA/proxy, then rebuild; see [Symptom B](#symptom-b--ssl-certificate_verify_failed) |
| `metadata-generation-failed` for `litellm` / Cargo 403             | litellm (or dep) installed from sdist; rustup blocked                               | See [Symptom D](#symptom-d--litellm-metadata-generation-failed-rustcargo-403)          |
| Lockfile churn on `jlpm install`                                   | System Yarn ≠ JupyterLab’s `jlpm`                                                   | Use `jlpm` only                                                                        |
| `Token<ILauncher>` / Lumino private-field TS error                 | Mixed `@jupyterlab/*` + `@lumino/coreutils` copies                                  | Align `@jupyterlab/*` ranges; cast optional tokens if needed (see `src/index.ts`)      |
| `labextension list` shows **X** / conflicting deps                 | Published `package.json` dependency range too tight (e.g. old `~4.2.0` on launcher) | Ship `^4.0.0` (or matching host) for shared `@jupyterlab/*` packages                   |
| `cryptography` / maturin hang on macOS x86_64                      | pip building from sdist                                                             | Use conda-forge wheel or a platform that has binary wheels                             |
| Importing NBI from the repo root fails with missing `_version`     | Local `notebook_intelligence/` shadows the install                                  | Run Python from another cwd, or ensure hatch version hook has run                      |
| Husky errors during `jlpm install` in CI                           | No `.git` or hook noise                                                             | `HUSKY=0 jlpm install`                                                                 |
| `pip install notebook` upgrades JupyterLab unexpectedly            | Notebook 7.6 pulls JL 4.6                                                           | Pin both: e.g. `jupyterlab==4.5.9` + `notebook==7.5.4` when testing 4.5                |

---

## Related files

| File                                                                | Role                                                        |
| ------------------------------------------------------------------- | ----------------------------------------------------------- |
| [`pyproject.toml`](../pyproject.toml)                               | Build backend, hatch hooks, wheel shared-data, runtime deps |
| [`package.json`](../package.json)                                   | Version, npm scripts, frontend deps, labextension config    |
| [`yarn.lock`](../yarn.lock)                                         | Locked frontend dependency graph                            |
| [`webpack.config.js`](../webpack.config.js)                         | Monaco + WASM for labextension build                        |
| [`install.json`](../install.json)                                   | Labextension package-manager metadata                       |
| [`jupyter-config/server-config/`](../jupyter-config/server-config/) | Auto-enable server extension                                |
| [`RELEASE.md`](../RELEASE.md)                                       | Manual + Jupyter Releaser publish steps                     |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md)                             | Editable install and tests                                  |
| [`.github/workflows/build.yml`](../.github/workflows/build.yml)     | CI build + package + isolated install                       |
