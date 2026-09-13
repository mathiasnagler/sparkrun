"""Compatibility for gateway plugins predating the typed model query contract.

The bundled SparkRoute snapshot still returns flat dictionaries. Older LiteLLM
adapters return wire-format dictionaries. Keep those translations below the API
boundary while new gateways implement query_models() directly.
"""

from collections.abc import Mapping

from .contracts import GatewayQueryError, ProxyModel


def model_rows(rows) -> tuple[ProxyModel, ...]:
    if not isinstance(rows, (list, tuple)):
        raise GatewayQueryError("Gateway returned an invalid model list")
    models = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise GatewayQueryError("Gateway returned an invalid model entry")
        info = row.get("model_info") or {}
        if not isinstance(info, Mapping):
            raise GatewayQueryError("Gateway returned invalid model metadata")
        params = row.get("litellm_params") or info.get("litellm_params") or {}
        if not isinstance(params, Mapping):
            raise GatewayQueryError("Gateway returned invalid model parameters")
        name = row.get("model_name", "?")
        base = row.get("api_base", params.get("api_base", ""))
        if not isinstance(name, str) or not isinstance(base, str):
            raise GatewayQueryError("Gateway returned an invalid model name or endpoint")
        length = row.get("max_model_len") or info.get("max_input_tokens") or info.get("max_tokens") or info.get("max_model_len")
        models.append(ProxyModel(name, base, length if isinstance(length, int) and not isinstance(length, bool) else None))
    return tuple(models)


def query_models(engine) -> tuple[ProxyModel, ...]:
    engine.model_query_error = ""
    try:
        rows = engine.list_models_via_api()
    except (NotImplementedError, OSError) as exc:
        raise GatewayQueryError(str(exc) or "Gateway cannot report its served models") from exc
    if engine.model_query_error:
        raise GatewayQueryError(engine.model_query_error)
    return model_rows(rows)
