"""One LLM turn, retried on transient gateway failures (5xx, timeouts, connection errors,
429 concurrency/rate limits). Shared by the full architect and the ablation arms so every arm
sees the same gateway resilience. Bounded by settings.llm_turn_retries."""
from __future__ import annotations

import time

from ..settings import settings


def create_with_retry(client, **kwargs):
    attempts = max(1, settings.llm_turn_retries + 1)
    for k in range(attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            name = type(exc).__name__
            transient = (status in (408, 409, 429) or (status is not None and status >= 500)
                         or "Timeout" in name or "Connection" in name)
            # An upstream timeout (some gateways cap one call at 300 s; cURL error 28) is
            # deterministic at temperature 0 — the same turn thinks too long again — so retry
            # it once, not four times (that cost ~50 min per affected cohort).
            upstream_timeout = status in (502, 504) and ("timed out" in str(exc).lower()
                                                         or "error 28" in str(exc).lower())
            budget = 1 if upstream_timeout else attempts - 1
            if not transient or k >= budget:
                raise
            # concurrency/rate limits need a real pause; 5xx/timeouts recover faster
            wait = min(120, 30 * (2 ** k)) if status == 429 else min(60, 5 * (2 ** k))
            print(f"      turn retry {k+1}/{budget} after {name}"
                  f"{' ' + str(status) if status else ''}; sleeping {wait}s", flush=True)
            time.sleep(wait)
