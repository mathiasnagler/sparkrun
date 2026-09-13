"""Malformed numeric metadata cannot prevent browsing neighboring recipes."""

import pytest
import yaml

from sparkrun import api
from test_api_catalog import catalog as catalog


@pytest.mark.parametrize("value", [10**400, -(10**400), float("nan"), float("inf"), -float("inf"), True, False, -1])
def test_invalid_numbers_are_omitted_without_losing_catalog_entries(catalog, value):
    context, root, data = catalog
    data["metadata"] = {"model_params": value, "benchmarks": [{"concurrency": value, "hardware": "test"}]}
    data["defaults"]["tensor_parallel"] = value
    (root / "bad-metadata.yaml").write_text(yaml.safe_dump(data))
    page = api.catalog_recipes(local_only=True, sctx=context)
    assert page["total"] == 3
    row = next(row for row in page["recipes"] if row["source_path"].endswith("bad-metadata.yaml"))
    assert row["parameters_b"] is None and row["tp"] is None
    assert row["benchmarks"] == [{"hardware": "test"}]


@pytest.mark.parametrize("value", [0, 2, 2.5, 10**308])
def test_valid_finite_numbers_keep_zero_only_for_benchmark_context(catalog, value):
    context, root, data = catalog
    data["metadata"] = {"model_params": value, "benchmarks": [{"concurrency": value}]}
    data["defaults"]["tensor_parallel"] = value
    (root / "numeric.yaml").write_text(yaml.safe_dump(data))
    page = api.catalog_recipes(local_only=True, sctx=context)
    row = next(row for row in page["recipes"] if row["source_path"].endswith("numeric.yaml"))
    assert row["parameters_b"] == (value / 1e9 if value else None)
    assert row["tp"] == (value if value else None)
    assert row["benchmarks"] == [{"concurrency": value}]
