"""Runtime display labels shared by metadata and CLI presentation."""

# Collapse vllm variants into a single display runtime for the website / metadata export.
RUNTIME_DISPLAY: dict[str, str] = {
    "vllm-distributed": "vllm",
    "vllm-ray": "vllm",
}
