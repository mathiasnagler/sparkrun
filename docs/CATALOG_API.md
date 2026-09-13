# Recipe catalog API

The catalog supports headless recipe browsing and selection through
`sparkrun.api`. It uses the application's configuration directory and registry
cache; it does not depend on the CLI or a particular gateway. Initialize a
context with `sparkrun.application.initialize()` and share it between calls.
See [application profiles](APPLICATION_PROFILES.md) for process/config binding.

## Supported imports and payloads

Functions and types below are exported by `sparkrun.api`. The `Catalog*` record
types are `TypedDict` definitions: returned values remain ordinary, mutable,
JSON-compatible dictionaries. They do not perform runtime validation or populate
missing fields. `NotRequired` fields may be absent; required nullable fields
are present with `None` when unknown. Consumers should tolerate added fields.
The wheel includes `py.typed` for consumers of inline annotations.

| Operation | Result type | Purpose |
| --- | --- | --- |
| `catalog_recipes(...)` | `CatalogPage` | Browse cached registry and controller-local recipes |
| `get_recipe_details(reference, overrides=None)` | `CatalogRecipeDetails` | Preview an exact selection, its validation issues, and trust |
| `import_recipe(content)` | `CatalogRecipeDetails` | Stage one YAML document and preview it |
| `resolve_catalog_recipe(reference, overrides=None)` | `ResolvedCatalogRecipe` | Return `(Recipe, normalized launch overrides)` |
| `list_registries()` | `list[CatalogRegistry]` | Configured registry inventory |
| `list_clusters()` | `list[CatalogCluster]` | Named clusters, without a capacity probe |
| `catalog_cluster_capacity(cluster)` | `CatalogCapacity` | Explicit live advisory occupancy query |
| `configure_registry(action, name, ...)` | `CatalogRegistryResult` | Explicit add/remove/enable/disable/trust/untrust operation |
| `refresh_registries(progress=None)` | `CatalogRefreshResult` | Explicit update with per-registry outcomes |
| `retain_catalog_recipe(reference)` | `None` | Keep a managed import for a persistent binding |
| `cleanup_catalog_imports(max_age_seconds=604800)` | `int` | Remove expired, unretained imports; return removal count |

All operations accept keyword-only `sctx=None`; an omitted context uses the
normal API initializer. `ResolvedCatalogRecipe` is a type alias for a tuple,
not a new result object: its `Recipe` member is a Python object, not a JSON
payload. Keep original requested overrides with a saved binding; image and env
overrides are applied to that recipe and removed from the normalized mapping.

## Browse, preview, and retain

This example prepares a binding without launching inference. `retain_catalog_recipe`
is relevant when your frontend commits a persistent selection: it is a no-op for
ordinary local/registry recipes. Store the reference, revision, and requested
overrides in your frontend's binding store after preparation succeeds.

```python
from sparkrun import api
from sparkrun.api import CatalogPage, CatalogRecipeDetails, ResolvedCatalogRecipe, SparkrunContext
from sparkrun.application import initialize
from sparkrun.orchestration.job_metadata import derive_recipe_fingerprint


def prepare_binding(
    reference: str, context: SparkrunContext
) -> tuple[CatalogRecipeDetails, ResolvedCatalogRecipe]:
    requested = {"tensor_parallel": "1"}
    details = api.get_recipe_details(reference, requested, sctx=context)
    if any(issue["severity"] == "error" for issue in details["issues"]):
        raise api.SparkrunError("Resolve the preview's validation/trust issues first")
    resolved = api.resolve_catalog_recipe(details["reference"], requested, sctx=context)
    recipe, normalized = resolved
    if derive_recipe_fingerprint(recipe, normalized) != details["recipe_revision"]:
        raise api.SparkrunError("Recipe changed during selection; preview it again")
    api.retain_catalog_recipe(details["reference"], sctx=context)
    return details, resolved


context = initialize()
page: CatalogPage = api.catalog_recipes(limit=10, sctx=context)
if page["recipes"]:
    details, resolved = prepare_binding(page["recipes"][0]["reference"], context)
    # Persist the reviewed reference/revision/requested overrides here.
```

Preparing/retaining a binding does not grant trust, execute hooks, build an image,
reserve hardware, or launch a workload. `resolve_catalog_recipe` alone is not a
validation/trust gate; use the preview issues and the normal launch validation.
Revalidate the saved revision when acting on a binding later. The example's
comparison detects a change between preview and resolution; it is not a lock
against subsequent file changes. Plugins may participate in parsing/validation,
so enabled plugin code must already be trusted by the application.

## Search and page fields

`catalog_recipes` accepts `query` (up to 256 characters), `registry`, `runtime`,
`local_only`, `offset` (nonnegative), `limit` (1–100, default 50), and `filters`.
An empty registry/runtime string means no such filter. Local roots are
`<config-directory>/recipes` and `<config-directory>/recipe-catalog/imports`,
independent of the process working directory. A registry filter excludes these
local roots; `local_only=True` excludes registry results. Enabled hidden
registries are included for explicit selection.

`catalog_recipes` and `list_registries` read cached recipe contents and local
inventory without network access, including first use. When `registries.yaml`
is absent, they use the application's fallback registries and plugin declarations;
manifest-only registries become available after an explicit `refresh_registries`.
Likewise, `search_recipes(ensure_initialized=False)` skips both manifest discovery
and registry synchronization. An offline read does not prevent later initialization
on the same context. Search may create reference records and cleans up expired
staged uploads.

| `CatalogPage` field | Meaning |
| --- | --- |
| `recipes: list[CatalogRecipe]` | Current page, deduplicated by canonical source path |
| `facets: dict[CatalogFacet, list[str]]` | Sorted values from all search candidates before applying facet filters |
| `total: int` | Number of matches after facet filtering, before pagination |
| `next_offset: int \| None` | Next page offset, or `None` when finished |
| `unavailable_registries: list[str]` | Enabled registries without a cache directory; not a live health probe |

Rows sort local entries before registry entries, then by name and path. Paging
is not a snapshot: refreshes or local edits between calls can change the page.

`CatalogRecipe` always includes `reference`, `source_path`, `name`, `model`,
`runtime`, `description`, `registry`, `min_nodes`, and `tp`. String summary fields
may be `None`; `registry=None` identifies a controller-local source. Metadata
adds optional `pp`, `quantization`, `context_length`, `parameters_b`, and
`benchmarks`. These are flattened in browse rows and nested under `metadata`
in details. When bounded metadata cannot be read, the lightweight summary's
raw `min_nodes`/`tp` values remain; those two browse fields are typed `object`
and require narrowing before numeric use. Browse rows are not validated recipes.

`CatalogFacet` is the shared literal vocabulary: `min_nodes`, `tp`, `pp`,
`quantization`, `context_length`, `parameters_b`. Filters are exact strings such
as `{"quantization": "fp8", "context_length": "32768"}`. Missing/null metadata
is displayed/filterable as `"unknown"`; an empty filter value is ignored. Each
filter value is limited to 128 characters. Facets come only from declared YAML
metadata/defaults, not model-name guesses, hardware probing, or model downloads.

## Details, identity, and validation

`CatalogRecipeDetails` has the following required fields:

| Fields | Meaning |
| --- | --- |
| `reference`, `source_path`, `name`, `registry` | Source identity, controller path, qualified name, optional registry name |
| `model`, `hf_model`, `runtime`, `description`, `min_nodes` | Serving name (or model), original model, runtime, description, minimum nodes |
| `defaults` | Selected raw recipe defaults: tensor/pipeline/data parallelism, max model length, GPU memory utilization, port, served model name; absent defaults are omitted |
| `metadata: CatalogRecipeMetadata` | Optional declared numeric/string facets and benchmark context |
| `recipe_revision` | Fingerprint of launch-relevant recipe inputs and normalized overrides |
| `plugin_items` | Exported plugin-owned items; their schemas belong to the owning plugins |
| `native_api_options`, `native_protocols`, `capabilities` | Runtime API choices and observed/declarative protocol/capability information |
| `required_plugins`, `available_plugins` | Registered recipe-item owners used by this recipe, and all registered recipe-item owners |
| `trusted`, `issues: list[CatalogIssue]` | Trust observation and validation/trust diagnostics |

A `catalog:...` reference is an opaque controller-local source identity, not a
copy of recipe contents or an authorization token. Registry references also
remember registry URL identity: removing/disabling a registry or changing its
URL makes old selections unavailable. Recipe file edits normally retain the
same reference and change `recipe_revision`. Fingerprints cover launch-relevant
inputs; passive plugin annotations need not change them. A reference alone does
not pin contents. Do not send `source_path` to remote nodes as if it existed there.

Selection also accepts an absolute controller-local path or an unambiguous cached
recipe name (including a registry scope). URL selections are rejected. Import
and exact resolution are limited to 256 KiB. Imports accept one valid YAML
mapping, store content under a hash-derived filename, and do not resolve build
assets. Imported recipes remain URL-sourced/untrusted even when reopened by their
staged absolute path. Imports requiring mods or builder configuration report an
auxiliary-file issue because a single document cannot carry those assets.

`CatalogIssue` always has `severity`, `code`, and `message`; `summary`, `fix`, and
`deprecation` are optional. Treat `severity="error"` as a blocking preview issue,
including trust requirements and unsupported/unknown plugin fields. Do not infer
trust from the absence of hooks in the displayed payload. `required_plugins`
is recipe-item ownership information, not a complete dependency resolver.

`CatalogRecipeMetadata` may be `{}` when unreadable/oversize, otherwise its facet
values may be `None`. `benchmarks` contains at most ten `CatalogBenchmarkContext`
records of declared throughput/latency/token/concurrency figures and descriptive
hardware/runtime/date/text fields. These are contextual claims supplied by a
recipe, not benchmark measurements produced by browsing.

## Import lifetime and registry changes

Successful previews (including re-imports of identical content) refresh staged
file age. Unretained uploads expire after seven days without a preview when
cleanup runs; catalog search triggers cleanup. `retain_catalog_recipe` prevents
expiry for a persistent binding without changing trust. Retention has no release
operation in this API; retained imports require deliberate administrative cleanup
when no longer needed. Do not retain every item merely because it was browsed.

Registry inventory records are `CatalogRegistry` dictionaries with `name`,
`enabled`, `visible`, `trusted`, and `cached`. Cluster inventory records are
`CatalogCluster` dictionaries with `name`, `description`, `host_count`, and
`default`. Neither is a live health check.

`configure_registry` accepts `add`, `remove`, `enable`, `disable`, `trust`, and
`untrust`; `add` accepts `url` and optional `subpath`. Adding does not clone or
grant trust. Trust requires `acknowledge_trust=True`; it should follow the
application's explicit review flow. The result wraps inventory as
`CatalogRegistryResult["registries"]`. `refresh_registries` returns
`CatalogRefreshResult` with `updated: dict[str, bool]` and `failed: list[str]`
for attempted registries. Its optional progress callback receives
`(registry_name, success)`; individual refresh failures remain in the result.

## Capacity and errors

`catalog_cluster_capacity("lab")` explicitly contacts the cluster's configured
executor/transport. `CatalogCapacity` contains `cluster`, `observed_at` (Unix
seconds), `hosts: list[CatalogHostCapacity]`, and `advisory=True`. Returned rows
are bounded to the first 256 configured hosts; the query may inspect the entire
cluster. There is no reservation or launch.

Each host row has `host`, `reachable`, `free_slots`, `used_slots`, and `workloads`.
A missing occupancy observation yields `reachable=False` and `None` for all
three counts, not zero available/used capacity. Reachability here means an
occupancy record was returned; it does not establish complete observation of
every backend. For detailed errors and backend coverage, use `api.status` and its
observation fields.

Selection failures use `RecipeNotFound`; invalid catalog requests, invalid recipe
resolution, and named-cluster lookup/configuration failures use `SparkrunError`.
Implicit initialization wraps bootstrap errors with their original cause.
Explicit `initialize()` exposes those bootstrap errors directly. Validation and
trust findings in a successful preview are returned in `issues`; refreshing
registries preserves per-registry failure outcomes. Interrupts propagate. See
[the API error contract](DISTRIBUTION_API_MIGRATION.md#errors-and-release-ownership).
