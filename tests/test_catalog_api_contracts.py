"""Typed catalog payloads and the documented headless selection lifecycle."""

import json
import os
from pathlib import Path
import re
import time
from typing import get_args, get_type_hints
from unittest.mock import Mock

import pytest
import yaml

from sparkrun import api
from sparkrun.core.cluster_status import ClusterStatus, HostOccupancy
from test_api_catalog import catalog as catalog


def test_sparse_metadata_preserves_dictionary_contract(catalog):
    context, root, data = catalog
    # A lightweight row remains browsable when metadata exceeds its read bound.
    data["description"] = "x" * (256 * 1024)
    data["defaults"]["tensor_parallel"] = "2"
    path = root / "oversize.yaml"
    path.write_text(yaml.safe_dump(data))
    page: api.CatalogPage = api.catalog_recipes(local_only=True, sctx=context)
    row: api.CatalogRecipe = next(row for row in page["recipes"] if row["source_path"] == str(path))
    assert row["tp"] == "2"  # Raw fallback has not been normalized into capacity.
    assert "pp" not in row and "benchmarks" not in row
    assert api.CatalogRecipe.__required_keys__ <= row.keys()
    assert {"pp", "benchmarks"} <= api.CatalogRecipe.__optional_keys__
    assert set(page["facets"]) == set(get_args(api.CatalogFacet))
    assert type(row) is dict and json.loads(json.dumps(page)) == page
    details: api.CatalogRecipeDetails = api.get_recipe_details(str(root / "one/same.yaml"), sctx=context)
    assert api.CatalogRecipeDetails.__required_keys__ <= details.keys()
    assert details["metadata"]["pp"] is None
    assert details["metadata"]["benchmarks"] == []
    assert json.loads(json.dumps(details)) == details
    assert get_type_hints(api.CatalogPage)["recipes"] == list[api.CatalogRecipe]


def test_capacity_payload_keeps_unknown_counts_and_cluster_configuration(catalog, monkeypatch):
    context, _, _ = catalog
    context.cluster_manager.update("lab", hosts=["10.0.0.1", "10.0.0.2"], executor="local")
    probe = Mock(return_value=ClusterStatus(hosts=(HostOccupancy(host="10.0.0.1", used_slots=1, free_slots=3),)))
    monkeypatch.setattr("sparkrun.api._status.status", probe)
    observed: api.CatalogCapacity = api.catalog_cluster_capacity("lab", sctx=context)
    assert probe.call_args.kwargs["cluster"].executor == "local"
    assert observed["hosts"] == [
        {"host": "10.0.0.1", "reachable": True, "free_slots": 3, "used_slots": 1, "workloads": 0},
        {"host": "10.0.0.2", "reachable": False, "free_slots": None, "used_slots": None, "workloads": None},
    ]
    assert observed["advisory"] is True
    assert api.CatalogCapacity.__required_keys__ <= observed.keys()
    assert all(api.CatalogHostCapacity.__required_keys__ <= host.keys() for host in observed["hosts"])
    assert json.loads(json.dumps(observed)) == observed


def test_documented_binding_flow_retains_without_granting_trust(catalog, monkeypatch):
    context, _, data = catalog
    remote = Mock(side_effect=AssertionError("preview launched a workload"))
    monkeypatch.setattr(api, "run", remote)
    guide = Path(__file__).resolve().parents[1] / "docs/CATALOG_API.md"
    source = re.search(r"```python\n(.*?)```", guide.read_text(), re.S)[1]
    namespace = {}
    exec(compile(source, str(guide), "exec"), namespace)
    prepare = namespace["prepare_binding"]
    uploaded = api.import_recipe(yaml.safe_dump(data), sctx=context)
    details, resolved = prepare(uploaded["reference"], context)
    assert not details["trusted"] and resolved[0].is_url_sourced
    assert type(resolved) is tuple and resolved[1]["tensor_parallel"] == 1

    data["pre_exec"] = ["echo this must not execute"]
    rejected = api.import_recipe(yaml.safe_dump(data), sctx=context)
    with pytest.raises(api.SparkrunError, match="validation/trust"):
        prepare(rejected["reference"], context)
    for selection in (uploaded, rejected):
        old = time.time() - 8 * 86400
        os.utime(selection["source_path"], (old, old))
    assert api.cleanup_catalog_imports(sctx=context) == 1
    assert Path(uploaded["source_path"]).exists()
    assert not Path(rejected["source_path"]).exists()
    remote.assert_not_called()
