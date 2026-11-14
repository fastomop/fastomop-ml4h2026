"""Model factory: a resolved provider config dict -> an agno model instance.

This is the "configurable by model + provider" core. `create_model` is the single
dispatch point; add a provider by adding a branch here and a `[models.providers.*]`
block in config.toml.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from agno.models.anthropic import Claude
from agno.models.azure import AzureOpenAI
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat


def create_model(config: Dict[str, Any]) -> Any:
    """Create an agno model from a resolved config dict.

    Expected keys (produced by config.get_agent_config / get_team_model_config):
      MODEL_TYPE: anthropic | openai | azure | ollama | ollama_openai | openai_compatible
      MODEL_ID:   provider-specific model id
      host, num_ctx, api_version, temperature, reasoning: optional, provider-dependent
    """
    model_type = config.get("MODEL_TYPE", "anthropic")
    model_id = config.get("MODEL_ID", "claude-3-5-sonnet-20241022")

    if model_type == "anthropic":
        return Claude(id=model_id)

    if model_type == "azure":
        return AzureOpenAI(
            id=model_id,
            api_version=config.get("api_version", "2024-10-21"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
            temperature=config.get("temperature"),
        )

    if model_type == "openai":
        return OpenAIChat(id=model_id)

    if model_type == "ollama":
        options: Dict[str, Any] = {}
        request_params: Dict[str, Any] = {}
        if config.get("num_ctx"):
            options["num_ctx"] = config["num_ctx"]
        # Sampling params. Without these, Ollama applies the MODEL CARD defaults (often
        # temperature 1.0 + a presence_penalty), which is not comparable with the hosted
        # providers that run at temperature 0. A presence_penalty is actively harmful in
        # this pipeline: it discourages verbatim copying of concept_codes out of tool
        # results. Default to greedy + no penalties so local and hosted arms match.
        options["temperature"] = config.get("temperature", 0.0)
        for opt, default in (("top_p", 1.0), ("top_k", 0), ("presence_penalty", 0.0),
                             ("frequency_penalty", 0.0), ("repeat_penalty", 1.0)):
            options[opt] = config.get(opt, default)
        if not config.get("reasoning", True):
            # `think` is a top-level ChatRequest field, NOT an options key.
            request_params["think"] = False
        return Ollama(
            id=model_id,
            host=config.get("host"),
            options=options or None,
            request_params=request_params or None,
        )

    if model_type == "ollama_openai":
        # Ollama's OpenAI-compatible endpoint (/v1) via agno's OpenAIChat. Bypasses
        # Ollama's native Harmony tool-call parser (buggy for gpt-oss models).
        host = config.get("host", "http://localhost:11434")
        base_url = f"{host.rstrip('/')}/v1"
        ollama_options: Dict[str, Any] = {}
        if config.get("num_ctx"):
            ollama_options["num_ctx"] = config["num_ctx"]
        if not config.get("reasoning", True):
            ollama_options["think"] = False
        return OpenAIChat(
            id=model_id,
            base_url=base_url,
            api_key="ollama",
            extra_body={"options": ollama_options} if ollama_options else None,
        )

    if model_type == "openai_compatible":
        # Any OpenAI-compatible gateway (vLLM, LiteLLM, a hosted endpoint, ...).
        # `base_url` is the full /v1 URL and lives in config; the key is read from the env var
        # named by `api_key_env` so it never lands in a tracked config file.
        base_url = config.get("base_url")
        if not base_url:
            raise ValueError("openai_compatible provider requires `base_url` in its [models.providers.*] block")
        api_key_env = config.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(
                f"openai_compatible provider needs the API key in ${api_key_env} (set it in .env)"
            )
        kwargs: Dict[str, Any] = {"id": model_id, "base_url": base_url, "api_key": api_key}
        for opt in ("temperature", "max_tokens"):
            if config.get(opt) is not None:
                kwargs[opt] = config[opt]
        return OpenAIChat(**kwargs)

    raise ValueError(f"Unknown model type: {model_type}")
