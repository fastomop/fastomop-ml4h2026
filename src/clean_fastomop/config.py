"""Configuration loading and resolution.

Loads config.toml (overlaid by an optional config.local.toml), then resolves each
agent's named provider into a flat dict the model factory understands. Also exposes
the two MCP server configs and the observability flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

try:  # Python 3.11+ has tomllib in the stdlib.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as _toml  # type: ignore[no-redef]

# Repo root = two levels up from this file (src/clean_fastomop/config.py).
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

CONFIG_PATH = ROOT_DIR / "config.toml"
LOCAL_CONFIG_PATH = ROOT_DIR / "config.local.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (override wins on leaves)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    """Load config.toml, overlaying config.local.toml if present."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "rb") as f:
        cfg = _toml.load(f)
    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH, "rb") as f:
            cfg = _deep_merge(cfg, _toml.load(f))
    return cfg


config = load_config()


def _resolve_provider(provider_key: str) -> Dict[str, Any]:
    """Flatten a [models.providers.<key>] block into factory keys."""
    providers = config["models"]["providers"]
    if provider_key not in providers:
        raise ValueError(f"Unknown model_provider '{provider_key}' (not in [models.providers])")
    p = providers[provider_key].copy()
    resolved: Dict[str, Any] = {"MODEL_TYPE": p["provider"], "MODEL_ID": p["model_id"]}
    for passthrough in (
        "host", "num_ctx", "api_version", "temperature",
        # openai_compatible: the gateway URL, the env var naming its key, and a token cap.
        "base_url", "api_key_env", "max_tokens",
        # ollama sampling overrides (default to greedy in models.py)
        "top_p", "top_k", "presence_penalty", "frequency_penalty", "repeat_penalty",
    ):
        if passthrough in p:
            resolved[passthrough] = p[passthrough]
    return resolved


def get_agent_config(agent_name: str) -> Dict[str, Any]:
    """Return an agent's config merged with its resolved provider.

    Includes agent fields (name, skills, reasoning, markdown, ...) plus MODEL_TYPE /
    MODEL_ID / host / num_ctx from the named provider (or default_provider).
    """
    if agent_name not in config.get("agents", {}):
        raise ValueError(f"Agent '{agent_name}' not found in [agents]")
    agent_config = config["agents"][agent_name].copy()
    provider_key = agent_config.get("model_provider", config["models"]["default_provider"])
    return {**agent_config, **_resolve_provider(provider_key)}


def get_team_model_config() -> Dict[str, Any]:
    """Resolve the default provider (for any team/orchestrator model)."""
    return _resolve_provider(config["models"]["default_provider"])


def get_mcp_config(name: str) -> Dict[str, Any]:
    """Return the [mcp.<name>] block (e.g. 'omcp' or 'concept_omcp')."""
    mcp = config.get("mcp", {})
    if name not in mcp:
        raise ValueError(f"MCP server '{name}' not found in [mcp]")
    return mcp[name].copy()


def observability_enabled() -> bool:
    return bool(config.get("observability", {}).get("enabled", False))


def get_validation_config() -> Dict[str, Any]:
    """Return the [validation] block (FastSSV pre-execution SQL gate) with defaults.

    enabled: gate the database agent's Select_Query with FastSSV (default True).
    strict:  run FastSSV in strict mode, escalating dangerous best-practice rules to ERROR.
    dialect: SQL dialect for static parsing (match the omcp database; default duckdb).
    policy:  "clinical" suppresses epi observation-window convention rules in the refinement
             loop; "epidemiological" enforces all rules (default clinical for this system).
    """
    v = config.get("validation", {})
    return {
        "enabled": v.get("enabled", True),
        "strict": v.get("strict", True),
        "dialect": v.get("dialect", "duckdb"),
        "policy": v.get("policy", "clinical"),
    }


def validate_skills(skills_dir: Path) -> None:
    """Check every skill referenced by every agent has a SKILL.md on disk."""
    missing = []
    for agent_name, agent in config.get("agents", {}).items():
        for skill in agent.get("skills", []):
            if not (skills_dir / skill / "SKILL.md").exists():
                missing.append(f"{agent_name} -> {skill}")
    if missing:
        raise FileNotFoundError(
            "Missing SKILL.md for: " + ", ".join(missing) + f" (looked under {skills_dir})"
        )


def get_governance_config() -> Dict[str, Any]:
    """[governance] block: whether the pre-generation screen halts the pipeline."""
    return {"enabled": bool(config.get("governance", {}).get("enabled", True))}
