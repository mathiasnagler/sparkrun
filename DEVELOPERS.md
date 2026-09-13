# Developer Guide

## Quick Start

```bash
git clone https://github.com/spark-arena/sparkrun.git -b develop-next
cd sparkrun
source dev.sh
sparkrun --help
```

`source dev.sh` uses `uv sync` to manage the `.venv` and install sparkrun with dev dependencies, then installs pre-commit hooks using `uv run pre-commit install`. After sourcing, the venv is activated in your current shell and `sparkrun` runs the code from your checkout — edits take effect immediately.

Requires [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

## Branch Model

| Branch | Purpose |
|--------|---------|
| `main` | Stable releases. Protected — no direct pushes |
| `develop` | Release staging; the source of the `beta` update channel |
| `develop-next` | Active integration; the source of the `alpha` update channel. **PRs target here** |
| `feature/*` | Feature branches off `develop-next` |

**All PRs should target `develop-next`**, not `develop` or `main`. Changes flow
`feature/*` → `develop-next` → `develop` → `main`.

## Running Tests

```bash
# Full suite
.venv/bin/python -m pytest tests/ -v

# Single file
.venv/bin/python -m pytest tests/test_recipe.py -v

# Single test
.venv/bin/python -m pytest tests/test_cli.py::test_run_command_basic -v

# With coverage
.venv/bin/python -m pytest tests/ --cov=sparkrun --cov-report=term-missing
```

All tests are self-contained — no real hosts, SSH, or Docker needed. SSH/Docker operations are mocked via `conftest.py` fixtures.

## Documentation contracts

The normal pytest suite also checks local Markdown links/anchors, Python example
syntax and Sparkrun imports, and documented command help. Run them directly with:

```bash
pytest -q tests/test_documentation_contracts.py
```

These checks are collected by the existing Python CI workflow. They perform no
network requests and do not execute example workloads. Historical release notes
are excluded from current-import checks; immutable vendor documentation is
verified by the vendor manifest. Keep the migration/import map and specialized
API guides authoritative, and link to them from overview documents.

## Linting

```bash
ruff check src/ tests/
ruff format src/ tests/
```

Config: `pyproject.toml` — line-length 140, target Python 3.12.

## Project Layout

```
src/sparkrun/
├── cli/              # Click CLI (one module per command group)
├── core/             # Config, recipe, registry, launcher, cluster manager
├── runtimes/         # Runtime plugins (vllm, sglang, llama-cpp, trtllm)
├── orchestration/    # SSH, Docker, executor, InfiniBand, scripts
├── builders/         # Builder plugins (eugr, docker-pull)
├── models/           # HuggingFace download, distribution, VRAM estimation
├── containers/       # Container image distribution
├── tuning/           # Triton kernel tuning (SGLang, vLLM)
├── benchmarking/     # Benchmark framework plugins
├── utils/            # Shared helpers
└── scripts/          # Embedded bash scripts (*.sh)
tests/                # pytest tests (mirrors src/ structure)
```

## Public API

Use `sparkrun.application.initialize()` for embedding and `sparkrun.api` for
operations. See the [0.4.0 migration guide](docs/DISTRIBUTION_API_MIGRATION.md)
for supported imports and the [benchmark guide](docs/BENCHMARK_API.md) for
measurement, integrations, and resume/finalization contracts.

## Key Patterns

### Plugin System (SAF)

Runtimes, builders, and benchmarking frameworks are SAF multi-extension plugins. Core implementations are scanned from bundled modules. Installed packages expose a module through the `sparkrun.plugins` entry-point group, declare `SPARKRUN_PLUGIN_API_VERSION = 1`, and are selected by the application profile or config. Per-kind Python entry-point groups are not consumed. See [plugin registration](docs/PLUGINS.md#installing-a-benchmark-framework). Bootstrap flow:

```
cli/__init__.py → core/bootstrap.py → SAF init → find_types_in_modules() → register_plugin()
```

### Config Chain (SAF Variables)

sparkrun uses SAF `Variables` with a `sources` tuple for cascading config resolution:

```python
from scitrera_app_framework.api import Variables, EnvPlacement

config = Variables(
    sources=(cli_overrides, user_config, recipe_defaults),
    env_placement=EnvPlacement.IGNORED,
)
value = config.get("port")  # resolves left-to-right; first non-None wins
```

Priority: CLI → user config → recipe defaults. `Recipe.build_config_chain()` (`core/recipe.py`) is the canonical entry point — pass your CLI override dict and it returns a ready-to-use `Variables` instance. The same pattern is used for executor config in `launcher.py`.

### Executor Abstraction

Workload operations go through the `Executor` ABC (`orchestration/executor.py`). Docker, native Local, and provider plugins implement the supported capabilities. Runtimes use `self.executor.*` instead of importing `docker.py` directly:

```python
# In a runtime:
self.executor.run_cmd(image, command, container_name=name, env=env)
self.executor.stop_cmd(container_name)
self.executor.generate_launch_script(image, container_name, command, ...)
self.executor.container_name(cluster_id, "solo")
self.executor.node_container_name(cluster_id, rank)
```

`resolve_executor()` builds the per-launch config through the [canonical precedence chain](docs/EXECUTORS.md#resolution-chain). Use that resolver so cluster, builder, runtime, application, and platform defaults remain consistent.

### SSH Execution Model

SSH-based execution uses **SSH stdin piping** — scripts are generated as Python strings and piped to `ssh host bash -s`. Supporting assets can be staged separately: Docker seccomp policies, for example, are distributed to every launch node before use.

```python
from sparkrun.orchestration.ssh import run_remote_script
result = run_remote_script(host, script_string, timeout=120, **ssh_kwargs)
```

### Shell Execution & Security

Sparkrun frequently dynamically generates bash scripts and Docker commands that interpolate user-provided inputs (like container names, image names, or environment variables). To prevent shell injection and handle spaces/special characters, you MUST adhere to the following rules:

1. **Use `sparkrun.utils.shell` helpers — not `shlex` directly**: All shell-quoting utilities live in `sparkrun.utils.shell`. Import from there rather than calling `shlex` directly:
   ```python
   from sparkrun.utils.shell import quote, quote_list, quote_dict, args_list_to_shell_str, render_args_as_flags

   # Single value
   cmd = f"docker run --name {quote(container_name)} {quote(image)}"

   # List of values → shell-safe space-separated string
   opts_str = args_list_to_shell_str(["--port", "8000", "--model", model_path])

   # List of values → quoted list (for further assembly)
   quoted = quote_list(["--tp", "2", model_path])

   # Dict with string values → quoted copy (e.g., env dicts)
   safe_env = quote_dict({"MODEL": model_path, "PORT": "8000"})

   # Dict of kwargs → ["--flag", "value", ...] pairs (booleans become bare flags)
   flags = render_args_as_flags({"tensor_parallel_size": 2, "enable_prefix_caching": True})
   ```

2. **Base64 Command Wrapping**: When passing complex commands (especially those with nested quotes or JSON) into `bash -c` or over SSH, use `b64_wrap_bash` or `b64_wrap_python` from `sparkrun.utils.shell`. These handle the full encode-and-pipeline internally:
   ```python
   from sparkrun.utils.shell import b64_wrap_bash, b64_wrap_python

   # Wrap a bash command (quoted=True by default — safe to embed in further shell strings)
   wrapped = b64_wrap_bash("vllm serve --hf-overrides '{\"rope\": \"yarn\"}'")
   # Produces (shell-quoted): printf '%s' '<b64>' | base64 -d -- | bash --noprofile --norc

   # Wrap a Python script for delivery over SSH
   wrapped_py = b64_wrap_python(python_script_str)
   # Produces (shell-quoted): printf '%s' '<b64>' | base64 -d -- | python3
   ```
   Use `b64_encode_cmd` only when you need raw base64 bytes and will construct the pipeline yourself.

3. **Use `printf` instead of `echo`**: Inside generated bash scripts (`.sh` files), never use `echo` to output interpolated Python variables. If a variable starts with a hyphen (e.g., `-n`), `echo` may interpret it as a flag. Instead, use `printf` with a format string:
   ```bash
   # DANGEROUS: echo "Launching {container_name}"
   # SAFE:
   printf "Launching %%s\n" "{container_name}"
   ```
   *Note: In Python string formatting (used to populate the scripts), `%` must be escaped as `%%`.*

4. **Environment Variables**: When exporting variables in generated bash scripts, quote the interpolated value using `quote` in Python and omit quotes in the bash script:
   ```python
   from sparkrun.utils.shell import quote
   env_lines.append(f"export MY_VAR={quote(val)}")
   ```

### Runtime Architecture

All runtimes extend `RuntimePlugin` (`runtimes/base.py`):

- `generate_command()` — produce the serve command from recipe + overrides
- `resolve_container()` — resolve the container image
- `run()` / `stop()` — solo/cluster dispatch (base class handles solo; subclasses implement `_run_cluster`)
- `cluster_strategy()` — `"ray"` or `"native"` determines orchestration path
- `get_extra_docker_opts()`, `get_extra_volumes()`, `get_extra_env()` — runtime-specific hooks

### Test Isolation

`conftest.py` provides an `isolate_stateful` autouse fixture that redirects SAF's stateful root to `tmp_path`. Tests never touch `~/.config/sparkrun/`. The bootstrap singleton is reset between tests.

## Adding a New Runtime

1. Create `src/sparkrun/runtimes/my_runtime.py` extending `RuntimePlugin`
2. Set `runtime_name = "my-runtime"` and `default_image_prefix`
3. Implement `generate_command()` and optionally `resolve_container()`
4. For multi-node: implement `_run_cluster()` and `_stop_cluster()`
5. Bundled runtime classes are discovered by the core module scan. For an installed plugin, export the class from a module declared under `[project.entry-points."sparkrun.plugins"]`, declare `SPARKRUN_PLUGIN_API_VERSION = 1`, and enable that plugin ID; see [the plugin guide](docs/PLUGINS.md).
6. Add tests in `tests/test_my_runtime.py`

## Adding a New Builder

1. Create `src/sparkrun/builders/my_builder.py` extending `BuilderPlugin`
2. Set `builder_name = "my-builder"`
3. Implement `prepare_image()` — must return the final image name
4. Bundled builders are scanned automatically. Installed builder packages use `sparkrun.plugins`, as described above
5. Recipes reference it as `builder: my-builder`

## Version Management

Versions are tracked in `versions.yaml` at the repo root:

```bash
# Sync versions across pyproject.toml and companion packages
python scripts/update-versions.py

# CI check (verify, don't write)
python scripts/update-versions.py --check
```

## Commit Guidelines

- Target the `develop-next` branch for all PRs
- Keep commits atomic — one logical change per commit
- Run `pytest` and `ruff check` before pushing
- Use `--dry-run` to verify CLI changes produce correct Docker commands

Python lint and formatting run in generated CI using the same pinned Ruff version
as the development environment and pre-commit hooks. Update `versions.yaml`,
`pyproject.toml`, and the hook revision together, then regenerate the workflows.
Vendor exclusions are centralized in `pyproject.toml` and apply to explicit
filenames too. Ruff formatting covers Python files; documentation examples are
checked by `tests/test_documentation_contracts.py`, which works without Git metadata.
