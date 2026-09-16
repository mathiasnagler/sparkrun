# Prepare a recipe without starting inference

`sparkrun build <recipe>` runs the recipe's builder, downloads and distributes
images and model files, and stages tuning configurations. It prepares Docker
recipes and native `executor: local` recipes, including `builder: uv-venv`.

The command is hidden from top-level help unless `SPARKRUN_ADVANCED=1` is set.
Like other hidden advanced commands, it can still be invoked directly.

~~~sh
SPARKRUN_ADVANCED=1 sparkrun build recipe.yaml --cluster sparks25
sparkrun build native-recipe.yaml --hosts localhost --executor local
sparkrun build recipe.yaml --cluster sparks25 --dry-run --json
~~~

Use the same recipe and overrides for the eventual `sparkrun run`. Build does
not rewrite the recipe or register a serving job.

## What is prepared

- The configured builder runs. Without a builder, existing container images
  are staged. For `uv-venv`, dependencies are installed and the activation file
  is written on each target host; an unchanged environment is reused.
- Images and model repositories, including auxiliary models declared by the
  runtime, use the existing distribution pipeline and per-resource revisions.
  Docker builds verify that every selected host has its expected image.
- Cluster transfer settings, model/tuning distribution preferences, and local
  and remote cache settings apply as they do at launch.
- Every selected host is prepared. Inference node-count limits and GPU
  occupancy do not trim the host list or prevent preparation.

This warms asset caches and environments. Model loading, GPU allocation, kernel
compilation/warmup, launch hooks, and ColdSnap capture/restore happen separately.
Build does not evict running jobs or clear the page cache. Builders may run
temporary build or probe processes.

For a ColdSnap recipe, build prepares the configured builder's image and recipe
assets without requiring an existing capsule. Core passes the runtime family as
builder context `engine`. Additional context can be provided using
`--builder-option snapshot_driver=n580`, for example. Preparing or distributing
a capsule itself requires explicit plugin support; launch-strategy hooks never
run during build. See [plugin build hooks](PLUGINS.md#preparation-only-build-hooks).

The initial supported executors are Docker and local. Delegating runtimes and
other executors return an explicit unsupported error. Native executor and
`uv-venv` feature flags still apply.

## Options and failures

Host selection uses `--hosts`, `--hosts-file`, `--cluster`, or the configured
default cluster. Common recipe overrides (`-o key=value`, image, parallelism,
memory, and context settings) are accepted.

`--cache-dir`, `--local-cache-dir`, `--transfer-mode`, and `--transfer-interface`
select the cache and transfer settings. Recipe `cluster_config` cache paths
retain their existing precedence. `--rebuild` / `--no-rebuild` set the builder's
rebuild preference; behavior depends on the builder.
`--no-sync-tuning` skips refreshing registry tuning files while still staging
existing local files. `--trust` authorizes recipe-owned hooks and host-path
overrides under the normal trust policy.

A builder, transfer, image verification, or tuning preparation failure exits
nonzero. Already completed downloads/builds are retained and reusable on retry.
`--dry-run` previews preparation without running builders or asset transfers;
its image references are intended values, not verified resident content.

## Python API

~~~python
from sparkrun import api
from sparkrun.application import initialize

sctx = initialize()
options = api.BuildOptions(
    recipe="recipe.yaml",
    cluster="sparks25",
    overrides={"max_num_seqs": 6},
)
plan = api.plan_build(options, sctx=sctx)
result = api.build(options, plan=plan, sctx=sctx)
~~~

`api.plan_build()` resolves the recipe, hosts, executor, and validation issues
without scheduling inference. Transport preparation may refresh connection
details. Passing its plan to `api.build()` reuses those targets; passing only
options plans and prepares in one call. A plan must use the same options.

`BuildResult` reports the recipe, hosts, executor, effective cache paths,
prepared image references, selected model repositories, native activation file
when available, dry-run status, and timings. Failures raise `SparkrunError`;
interrupts propagate.

`api.materialize(RunOptions(...))` keeps its existing meaning: resolve a launch
specification without building or distributing resources. Use `api.build()`
for preparation and `api.run()` for activation.
