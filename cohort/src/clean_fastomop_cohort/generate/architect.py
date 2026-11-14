"""Back-compat shim.

The cohort architect is now a multi-agent, hierarchy-aware pipeline (generate/agents.py):
a Codeset Builder that reasons about concept-set granularity via the vocabulary hierarchy, and
a Cohort Critic that executes/estimates and judges medical face validity. `CleanArchitect`
keeps the original constructor + `generate()` contract so experiment.py is unchanged.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..resources.concepts import ConceptResolver
from .agents import MultiAgentArchitect


class CleanArchitect:
    def __init__(self, resolver: Optional[ConceptResolver] = None, executor=None,
                 cdm=None, max_iters: int = 16):
        self._impl = MultiAgentArchitect(resolver=resolver, executor=executor, cdm=cdm,
                                         max_iters=max_iters)

    def generate(self, description: str) -> Dict[str, Any]:
        return self._impl.generate(description)
