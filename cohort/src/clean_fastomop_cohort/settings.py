"""Central config — loads .env once and exposes the four resource locations."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from the repo root (two parents up from this file) if present.
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    duckdb_path: str = os.getenv("DUCKDB_PATH", "")
    cdm_schema: str = os.getenv("CDM_SCHEMA", "omop_gold")
    vocab_schema: str = os.getenv("VOCAB_SCHEMA", "athena")
    # concept-omcp (the terminology service) is an installed Python
    # package. CONCEPT_OMCP_ARTIFACT_DIR is read by its own settings (pydantic-settings, prefix
    # CONCEPT_OMCP_) once load_dotenv above has put it in the environment. CONCEPT_MCP_SRC is a
    # legacy fallback for a source checkout that is not installed.
    concept_mcp_src: str = os.getenv("CONCEPT_MCP_SRC", "")
    concept_artifact_dir: str = os.getenv("CONCEPT_OMCP_ARTIFACT_DIR", "")
    gold_root: str = os.getenv("GOLD_ROOT", "")
    # How the prompts describe the target database to the model. The defaults are the wording
    # used for the paper's runs; set both for another database. The Critic's broadening rule
    # (generate/agents.py, TRIGGER 2) is additionally tuned to common ICU diagnoses.
    cdm_description: str = os.getenv("CDM_DESCRIPTION", "a MIMIC ICU OMOP database (~65k patients)")
    cdm_short_description: str = os.getenv("CDM_SHORT_DESCRIPTION", "a ~65k ICU DB")
    circe_tool_dir: str = os.getenv(
        "CIRCE_TOOL_DIR", str(_REPO_ROOT / "tools" / "circe-compiler"))
    java_bin: str = os.getenv("JAVA_BIN", "java")
    azure_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    azure_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
    azure_deployment: str = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    # Public OpenAI (api.openai.com) — used when set. The org's Azure endpoint is a
    # private VNet endpoint that does NOT resolve from a local machine, so for the
    # local experiment a public OPENAI_API_KEY is the reachable path.
    openai_key: str = os.getenv("OPENAI_API_KEY", "")
    # Any OpenAI-compatible endpoint (vLLM, LiteLLM, a hosted gateway).
    # Empty -> api.openai.com.
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    # Per-request timeout (s) and retries for the architect's LLM calls. The SDK default is
    # 600 s x 2 retries, so one hung gateway request could stall a cohort for 30 min.
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT_S", "180"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "1"))
    # Loop-level retries of one architect turn on transient gateway errors (5xx/timeouts/429),
    # on top of the SDK's own; each attempt can take up to LLM_TIMEOUT_S.
    llm_turn_retries: int = int(os.getenv("LLM_TURN_RETRIES", "4"))
    # Ollama only: context window to request per call, sent as extra_body {"options": {"num_ctx"}}
    # through the /v1 endpoint (as the NL2SQL pipeline does). 0 = don't send.
    # Ollama's default context is far smaller than the ~4.3k-token first Builder call, so set it.
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "0"))
    # LLM_API=ollama -> talk to Ollama's native /api/chat (host = OPENAI_BASE_URL without /v1),
    # which honours think/num_ctx; the /v1 OpenAI layer silently drops both. Default: openai.
    llm_api: str = os.getenv("LLM_API", "openai").lower()
    # Ollama only: LLM_THINK=0 disables the model's hidden reasoning (gemma4 / qwen3.x think by
    # default; on the A100 that was ~140 s/turn vs ~4 s without). Sent both as a top-level
    # `think` field (what /api/chat takes) and as options.think, so whichever form Ollama's
    # /v1 layer forwards takes effect. Unset = model default.
    llm_think: Optional[bool] = (None if os.getenv("LLM_THINK", "") == ""
                                 else os.getenv("LLM_THINK") not in ("0", "false", "False"))
    # Architect budget: Critic review rounds per cohort (each round = a Critic loop + a Builder
    # refinement loop). 3 = the gpt-4.1 baseline protocol; 1 = cheapest faithful variant.
    critic_max_rounds: int = int(os.getenv("CRITIC_MAX_ROUNDS", "3"))
    # Print one line per LLM turn (model, tools called, prompt tokens) — progress on slow models.
    llm_trace: bool = os.getenv("LLM_TRACE", "0") == "1"
    # DuckDB session limits (empty = DuckDB defaults). A small database node needs a cap so
    # DuckDB does not fight the terminology service's embedder for RAM.
    duckdb_memory_limit: str = os.getenv("DUCKDB_MEMORY_LIMIT", "")
    duckdb_temp_dir: str = os.getenv("DUCKDB_TEMP_DIR", "")
    duckdb_max_temp_size: str = os.getenv("DUCKDB_MAX_TEMP_SIZE", "")
    # Optional per-agent model overrides for the multi-agent architect; empty -> use the base
    # model above (so both agents run gpt-4.1 today, but a stronger Critic is a config flip).
    openai_model_builder: str = os.getenv("OPENAI_MODEL_BUILDER", "")
    openai_model_critic: str = os.getenv("OPENAI_MODEL_CRITIC", "")
    azure_deployment_builder: str = os.getenv("AZURE_OPENAI_DEPLOYMENT_BUILDER", "")
    azure_deployment_critic: str = os.getenv("AZURE_OPENAI_DEPLOYMENT_CRITIC", "")

    @property
    def llm_extra_body(self) -> Optional[dict]:
        body: dict = {}
        opts: dict = {}
        if self.ollama_num_ctx:
            opts["num_ctx"] = self.ollama_num_ctx
        if self.llm_think is not None:
            body["think"] = self.llm_think
            opts["think"] = self.llm_think
        if opts:
            body["options"] = opts
        return body or None

    @property
    def llm_model(self) -> str:
        """The architect model actually in use (mirrors the client choice in agents.py)."""
        return self.openai_model if (self.openai_key or self.llm_api == "ollama") else self.azure_deployment

    def make_client(self):
        """The one place that decides how the architect (and the CLI) talk to the LLM."""
        if self.llm_api == "ollama":
            from .generate.ollama_client import OllamaChatClient
            return OllamaChatClient(self.openai_base_url or "http://localhost:11434",
                                    timeout=self.llm_timeout, think=self.llm_think,
                                    num_ctx=self.ollama_num_ctx)
        from openai import AzureOpenAI, OpenAI
        if self.openai_key:
            return OpenAI(api_key=self.openai_key, base_url=self.openai_base_url or None,
                          timeout=self.llm_timeout, max_retries=self.llm_max_retries)
        if self.azure_endpoint and self.azure_key:
            return AzureOpenAI(azure_endpoint=self.azure_endpoint, api_key=self.azure_key,
                               api_version=self.azure_api_version,
                               timeout=self.llm_timeout, max_retries=self.llm_max_retries)
        raise RuntimeError("No reachable LLM configured (set OPENAI_API_KEY, LLM_API=ollama, or AZURE_OPENAI_*).")

    @property
    def llm_endpoint(self) -> str:
        if self.llm_api == "ollama":
            return (self.openai_base_url or "http://localhost:11434").rstrip("/").removesuffix("/v1") + "/api/chat"
        if self.openai_key:
            return self.openai_base_url or "https://api.openai.com/v1"
        return self.azure_endpoint

    def ensure_concept_mcp_on_path(self) -> None:
        """Legacy: make `import concept_omcp` work from a source checkout that is not installed."""
        if self.concept_mcp_src and self.concept_mcp_src not in sys.path:
            sys.path.insert(0, self.concept_mcp_src)


settings = Settings()
