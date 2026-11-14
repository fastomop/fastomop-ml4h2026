"""Optional observability — a no-op unless [observability].enabled is true in config.

Keeps Langfuse/OpenLIT out of the hot path. When enabled (and the `observability`
extra is installed), `setup_observability` wires up agno's OpenLIT/Langfuse tracing;
otherwise it does nothing.
"""

from __future__ import annotations

from clean_fastomop.config import observability_enabled


def setup_observability() -> bool:
    """Initialize tracing if enabled. Returns True if tracing was set up."""
    if not observability_enabled():
        return False
    try:
        import openlit  # type: ignore

        openlit.init()
        return True
    except Exception as exc:  # pragma: no cover - optional dependency / config
        print(f"[observability] enabled but setup failed ({exc}); continuing without tracing")
        return False
