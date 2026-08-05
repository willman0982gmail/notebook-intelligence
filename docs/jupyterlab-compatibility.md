# JupyterLab Compatibility

This document records Notebook Intelligence (NBI) compatibility with JupyterLab **4.4.x**, **4.5.x**, and **4.6.x**, based on a review of declared dependencies, frontend/server APIs in use, the JupyterLab extension migration guide, and CI configuration.

Review date: 2026-08-05. NBI version under review: **5.3.1**.

---

## Summary

| JupyterLab | Prebuilt wheel (pip install)            | Build from source | Formally tested in CI | Verdict                                                                             |
| ---------- | --------------------------------------- | ----------------- | --------------------- | ----------------------------------------------------------------------------------- |
| **4.4.x**  | Expected to work                        | No                | No                    | Compatible in principle; not a validated target                                     |
| **4.5.x**  | **Blocked by launcher pin** (see below) | Yes (`>=4.5.7`)   | Indirectly            | **Server OK; frontend marked incompatible** until `@jupyterlab/launcher` is widened |
| **4.6.x**  | Likely same launcher conflict           | Yes (`<5`)        | Indirectly (latest)   | Same launcher-pin issue expected; see [rebuild notes](#jupyterlab-46x)              |

NBI declares **JupyterLab 4.x** support (see [README Requirements](../README.md#requirements) and PyPI classifiers). It does **not** pin a JupyterLab minor as a runtime Python dependency; JupyterLab is provided by the environment, and NBI ships as a prebuilt labextension plus a `jupyter_server` `ExtensionApp`.

### Empirical test (2026-08-05): JupyterLab 4.5.9 + NBI 5.3.1

Environment: conda env `nbi-jl45`, Python 3.12.

```bash
conda create -n nbi-jl45 python=3.12
conda activate nbi-jl45
conda install -c conda-forge cryptography   # avoid source-build hang on macOS x86_64
pip install "jupyterlab==4.5.9" "notebook==7.5.4" "notebook-intelligence"
```

| Check                                                                                     | Result                                                                                     |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `jupyterlab` / `notebook`                                                                 | 4.5.9 / 7.5.4                                                                              |
| `notebook_intelligence` server extension                                                  | `enabled` / `OK`                                                                           |
| `@plmbr/notebook-intelligence` labextension (stock wheel)                                 | `enabled` / **`X` (incompatible)**                                                         |
| Conflict (`jupyter labextension list --verbose`)                                          | Host `@jupyterlab/launcher` `>=4.5.9 <4.6.0` vs extension `>=4.2.0 <4.3.0`                 |
| After patching installed labextension `package.json` → `"@jupyterlab/launcher": "^4.0.0"` | labextension `enabled` / `OK`; present in page `federated_extensions`; Copilot WS connects |

Note: `pip install notebook` without a pin can pull Notebook 7.6.x and upgrade JupyterLab to 4.6.x. Keep `notebook==7.5.x` when testing JupyterLab 4.5.x.

**Conclusion:** the `~4.2.0` pin on `@jupyterlab/launcher` is published in the wheel’s labextension metadata and blocks stock NBI 5.3.1 on JupyterLab 4.5.x (same semver logic applies to 4.4.x / 4.6.x). Widen to `^4.0.0` and release.

---

## Declared constraints

### Runtime (what end users install)

| Constraint       | Source                                | Value                                             |
| ---------------- | ------------------------------------- | ------------------------------------------------- |
| JupyterLab       | README / classifiers                  | `4.x` (`Framework :: Jupyter :: JupyterLab :: 4`) |
| `jupyter_server` | [`pyproject.toml`](../pyproject.toml) | `>=2.20.0,<3`                                     |
| Python           | [`pyproject.toml`](../pyproject.toml) | `>=3.10`                                          |

There is **no** `jupyterlab` entry under `[project].dependencies`. Installing `notebook-intelligence` does not install JupyterLab; the host environment must already provide JupyterLab 4.x.

### Build-time (building the labextension from source)

| Constraint                          | Source                                | Value                                |
| ----------------------------------- | ------------------------------------- | ------------------------------------ |
| `jupyterlab` (PEP 517 build-system) | [`pyproject.toml`](../pyproject.toml) | `>=4.5.7,<5`                         |
| `@jupyterlab/builder`               | [`package.json`](../package.json)     | `^4.5.9`                             |
| Most `@jupyterlab/*` frontend deps  | [`package.json`](../package.json)     | `^4.0.0`                             |
| `@jupyterlab/launcher`              | [`package.json`](../package.json)     | `~4.2.0` (build-time pin; see below) |
| jupyter-releaser hook               | [`pyproject.toml`](../pyproject.toml) | installs `jupyterlab>=4.0.0,<5`      |

The build-system floor of `jupyterlab>=4.5.7` was raised for security fixes (CVE-2026-42266 / CVE-2026-42557), not because the frontend code requires 4.5 APIs. **Source builds against JupyterLab 4.4.x are therefore unsupported**, even when a prebuilt wheel may still run on 4.4.x.

### CI

[`.github/workflows/build.yml`](../.github/workflows/build.yml) installs `jupyterlab>=4.0.0,<5` (no minor-version matrix). On a fresh runner that currently resolves to the latest JupyterLab 4.x release (4.6.x as of this review). There is **no** dedicated job that pins 4.4, 4.5, and 4.6 separately.

---

## Frontend API review

NBI’s labextension uses standard JupyterLab 4 plugin tokens and APIs:

| API / token                                             | Package                                  | Required?           | Present in 4.4+                |
| ------------------------------------------------------- | ---------------------------------------- | ------------------- | ------------------------------ |
| `JupyterFrontEnd` / plugin registration                 | `@jupyterlab/application`                | Required            | Yes                            |
| `ICompletionProviderManager`, `IInlineCompleterFactory` | `@jupyterlab/completer`                  | Required / optional | Yes                            |
| `IDocumentManager`, `DocumentWidget`                    | `@jupyterlab/docmanager` / `docregistry` | Required            | Yes                            |
| `IDefaultFileBrowser`, `FileDialog`                     | `@jupyterlab/filebrowser`                | Required            | Yes                            |
| `IMainMenu`, `ICommandPalette`                          | `@jupyterlab/mainmenu` / `apputils`      | Required            | Yes                            |
| `ISettingRegistry`                                      | `@jupyterlab/settingregistry`            | Used                | Yes                            |
| `IStatusBar`                                            | `@jupyterlab/statusbar`                  | Optional            | Yes                            |
| `ILauncher`                                             | `@jupyterlab/launcher`                   | Optional            | Yes                            |
| `ITerminalTracker`                                      | `@jupyterlab/terminal`                   | Optional            | Yes                            |
| `Notification`                                          | `@jupyterlab/apputils`                   | Used                | Yes (exported since early 4.x) |
| `NotebookPanel`, `NotebookActions`, `CodeCell`          | `@jupyterlab/notebook` / `cells`         | Used                | Yes                            |
| `ISharedNotebook` (type only)                           | `@jupyter/ydoc`                          | Type annotation     | Yes (v3 and v4)                |

Optional tokens (`ILauncher`, `ITerminalTracker`, `IStatusBar`, `IInlineCompleterFactory`) are declared under `optional` in `src/index.ts`, so a missing provider degrades a feature (for example, coding-agent launcher tiles) rather than blocking activation.

### APIs called out in the JupyterLab migration guide

Cross-checked against the [Extension Migration Guide](https://jupyterlab.readthedocs.io/en/latest/extension/extension_migration.html) for 4.3→4.4, 4.4→4.5, and 4.5→4.6:

| Migration item                                            | NBI impact                                                                                                                                                                                                               |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `renameDialog` deprecated (4.5)                           | Not used                                                                                                                                                                                                                 |
| `DirListing` / `selectionChanged` (4.5)                   | NBI does not subclass `DirListing`                                                                                                                                                                                       |
| `IDefaultContentProvider` (4.5.0/4.5.1)                   | Not used                                                                                                                                                                                                                 |
| Debugger `currentFrameChanged` deprecation (4.6)          | Not used                                                                                                                                                                                                                 |
| Cell `syncEditable` / `syncCollapse` default change (4.6) | NBI does not provide a custom notebook content factory                                                                                                                                                                   |
| `@jupyter/ydoc` v4 singleton (4.6)                        | NBI does **not** declare `@jupyter/ydoc` as a direct dependency; `ISharedNotebook` is used only as a TypeScript type. No federated ydoc version conflict is expected (unlike collaboration extensions that pin ydoc v3). |
| Custom Webpack → Rspack (4.6)                             | Relevant only when **rebuilding** against a 4.6 builder; see below                                                                                                                                                       |

### `@jupyterlab/launcher` pin (`~4.2.0`) — known blocker

`package.json` pins `@jupyterlab/launcher` to `~4.2.0` to avoid a build-time `@lumino/coreutils` `Token<ILauncher>` mismatch when Yarn resolves launcher 4.5.x alongside older Lumino copies.

That pin is **also published** in the labextension’s `package.json` inside the wheel. JupyterLab’s compatibility checker then compares it to the host’s launcher range and rejects the extension on JupyterLab ≥ 4.3:

```text
Conflicting Dependencies:
JupyterLab           Extension      Package
>=4.5.9 <4.6.0       >=4.2.0 <4.3.0 @jupyterlab/launcher
```

`ILauncher` is an optional token in NBI, but the metadata check still marks the whole labextension incompatible, so it is not loaded as a federated extension.

**Recommended fix:** change `@jupyterlab/launcher` to `^4.0.0` (matching the other `@jupyterlab/*` deps), rebuild the labextension against a consistent JupyterLab 4.5+/4.6 toolchain, and release. Until then, a local workaround is to edit the installed file:

`share/jupyter/labextensions/@plmbr/notebook-intelligence/package.json`

and set `"@jupyterlab/launcher": "^4.0.0"`.

---

## Server extension review

The Python side registers a `jupyter_server.extension.application.ExtensionApp` (`NotebookIntelligence` in `notebook_intelligence/extension.py`). That pattern is stable across JupyterLab 4.x / `jupyter_server` 2.x.

Runtime floor `jupyter_server>=2.20.0` is independent of the JupyterLab minor and is satisfied by current JupyterLab 4.4–4.6 dependency stacks that pull `jupyter_server` 2.x.

---

## Per-version notes

### JupyterLab 4.4.x

**Runtime (prebuilt):** Expected compatible. Frontend deps are declared as `^4.0.0`, and the tokens/APIs NBI requires exist in 4.4. Optional features fail soft if a token is unavailable.

**Source build:** Not supported. `[build-system]` requires `jupyterlab>=4.5.7,<5`.

**Testing gap:** CI does not pin 4.4. Treat production use on 4.4.x as best-effort unless your deployment validates it.

### JupyterLab 4.5.x

**Supported.** This is the intended build and development target:

- Build-system lower bound `>=4.5.7` (security floor).
- `@jupyterlab/builder` `^4.5.9`.
- Dev lockfile / local environments resolve around the 4.5 line.
- No 4.4→4.5 migration items affect NBI’s code paths.

Prefer **4.5.7+** (or the latest 4.5.x patch) in images that still track the 4.5 line.

### JupyterLab 4.6.x

**Runtime (prebuilt):** Supported. Upstream states that existing extensions continue to work on 4.6; NBI does not use the APIs that changed in breaking or high-risk ways for this release. CI’s open `>=4.0.0,<5` range installs the latest 4.x (currently 4.6.x) for `browser_check` and extension-list checks.

**Source rebuild caveats:**

1. **Custom Webpack config** — [`webpack.config.js`](../webpack.config.js) adds `monaco-editor-webpack-plugin` and `experiments.syncWebAssembly`. JupyterLab 4.6 can build extensions with Rspack. Most extensions need no changes; custom Webpack config may need a [Rspack migration](https://rspack.rs/guide/migration/webpack) if you rebuild NBI with a 4.6-era builder and the Monaco/Wasm plugins fail.
2. **`jupyter-builder` migration (recommended, not required)** — Upstream recommends replacing the `jupyterlab` build dependency with [`jupyter-builder`](https://pypi.org/project/jupyter-builder/) / `@jupyter/builder`. NBI still uses `@jupyterlab/builder` and `jupyter labextension build`; that remains valid.
3. **`setuptools`** — JupyterLab 4.6 no longer depends on `setuptools` at runtime. NBI’s packaging uses Hatchling (`pyproject.toml`); the root `setup.py` is a thin setuptools shim for legacy tooling and does not make `setuptools` a runtime requirement of the installed wheel.

---

## Verification checklist for deployers

Use this when validating a specific JupyterLab minor in your image:

1. Install JupyterLab (pin the minor you care about) and `notebook-intelligence`.
2. Confirm both halves load:
   ```bash
   jupyter server extension list   # notebook_intelligence ... OK
   jupyter labextension list       # @plmbr/notebook-intelligence ... OK
   ```
3. Open JupyterLab and check:
   - Chat sidebar activates (left rail).
   - Settings dialog opens.
   - Inline completer / chat still function with your provider.
   - Coding-agent launcher tiles appear when the matching CLIs are on `PATH` (requires `ILauncher`).
4. Watch the browser console for shared-singleton warnings (especially `@jupyter/ydoc` if other collaboration extensions are installed).
5. For source builds, use JupyterLab **≥ 4.5.7**.

---

## Recommendations

1. **Release blocker:** Change `@jupyterlab/launcher` from `~4.2.0` to `^4.0.0` in [`package.json`](../package.json), rebuild, and publish a patch. Until then, stock PyPI wheels fail the labextension compatibility check on JupyterLab ≥ 4.3.
2. **Production (after the launcher fix):** JupyterLab **4.5.7+** or **4.6.x**, with `jupyter_server>=2.20.0`. Pair Notebook **7.5.x** with JL 4.5.x and **7.6.x** with JL 4.6.x.
3. **4.4.x:** Acceptable only after the launcher fix and local validation; do not expect to build NBI from source on 4.4.
4. **CI improvement (future work):** Add a matrix job for `jupyterlab==4.4.*`, `4.5.*`, and `4.6.*` so minor support is continuously proven rather than inferred.
5. **Follow-ups for 4.6 builds:** Evaluate migrating to `jupyter-builder` / `@jupyter/builder`, and confirm the Monaco Webpack plugin path under Rspack before cutting release artifacts on a 4.6-only builder.

---

## Related docs

- [Administrator Guide — Version matrix](admin-guide.md#version-matrix) (NBI release lines vs JupyterLab 4.x)
- [Troubleshooting](troubleshooting.md)
- [Contributing](../CONTRIBUTING.md) (building from source)
