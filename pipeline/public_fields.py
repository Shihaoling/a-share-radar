"""Remove private provider accounting from site publications in memory."""

PRIVATE_COUNTERS = {
    "usage", "model_usage", "token_usage", "tokens", "token_limit", "calls", "requests",
    "request_count", "call_count", "requests_this_run", "collection_attempts",
}


def remove_usage(value):
    """Modify the decoded publication only. Text values and market data are untouched."""
    if isinstance(value,dict):
        for key in tuple(value):
            if key in PRIVATE_COUNTERS or key.endswith(("_tokens","_calls","_requests")):
                del value[key]
            else:
                remove_usage(value[key])
    elif isinstance(value,list):
        for item in value:
            remove_usage(item)
    return value
