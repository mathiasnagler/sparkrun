"""The inline-typed API must work for consumers outside this repository."""

from _api_typecheck import check_api_consumer


def test_catalog_consumer_types(tmp_path):
    check_api_consumer(tmp_path)
