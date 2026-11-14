"""Multi-agent, hierarchy-aware cohort architect.

Two gpt-4.1 agents share ONE CohortBuilder:
  * Codeset Builder — turns the description into a cohort, REASONING about concept-set
    granularity via the hierarchy tools (walk up/down, see CDM coverage) instead of a
    hard-coded resolver heuristic; follows the OHDSI structural rules (OR'd entry events,
    AND'd inclusion rules, per-arm criteria, washout, lab values).
  * Cohort Critic — executes/estimates the assembled cohort (attrition chart), judges medical
    face validity, and either accepts or hands a concrete refinement back to the Builder, to
    overcome cohort collapse and too-narrow/too-broad granularity.

Replaces CleanArchitect.generate()'s single loop; the return contract is unchanged so
experiment.py is untouched.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..settings import settings
from ..resources.concepts import ConceptResolver
from ..resources.hierarchy import HierarchyResource
from .tools import CohortToolset, tool_schemas
from .agent_tools import AgentToolset, AGENT_TOOLS, builder_extra_schemas, critic_schemas
from .llm_retry import create_with_retry

_CORE_DISPATCH = {
    "start_cohort", "add_concept_set", "add_primary_criteria",
    "add_demographic_rule", "add_event_rule", "add_arm_correlated_criteria", "finalize",
}

MAX_ROUNDS = settings.critic_max_rounds  # env CRITIC_MAX_ROUNDS, default 3
MAX_BUILDER_ITERS = 16
MAX_REFINE_ITERS = 8
MAX_CRITIC_ITERS = 12

BUILDER_PROMPT = """You are an OMOP cohort architect. Turn the clinical description into ONE executable
OMOP cohort definition by calling the tools — fully autonomously, never ask questions.

GRANULARITY IS YOUR KEY JOB (OHDSI 'concept set' practice). Source data is coded for operations, not
research, so the literal phenotype name is often coded at a DIFFERENT level than you'd expect. For each
phenotype:
- lookup_concept(name, domain) to see candidate standard concepts AND how many CDM patients each covers.
- Always walk the hierarchy before committing: get_ancestors (does the data code a broader parent
  FAMILY?) and get_descendants (review the children; which actually carry patients).
- GRANULARITY — DEFAULT TO THE SPECIFIC CONCEPT the description names (with descendants). Only bridge UP
  to a broader SAME-DISEASE family when the literal concept covers IMPLAUSIBLY FEW patients for a COMMON,
  high-prevalence condition (e.g. 'Left heart failure' ~282 when heart failure should be tens of thousands
  in a ~65k ICU DB). When you do bridge: drop ONLY a laterality/acuity/site qualifier, NEVER jump to a
  generic CATEGORY that merges clinically DIFFERENT diseases (not 'Anemia' for sickle-cell; not a stroke
  parent that adds hemorrhagic for ISCHEMIC stroke); when subtypes exist, assemble the on-disease siblings
  (get_children of the shared parent, pick the on-disease ones). DO NOT broaden a SPECIFIC or rare
  phenotype — a named cancer/subtype, a rare syndrome, a procedure, a 'type X' — its small size is CORRECT.
  Use preview_codeset to sanity-check: a 0-patient codeset is wrong, but a small count for a specific
  phenotype is fine. When unsure, prefer the more specific concept.
- Commit with add_concept_set_by_ids(name, concept_ids, domain, exclude_ids?) for the exact concepts you
  chose (or add_concept_set for a simple single term); use replace_concept_set_by_ids(codeset_id, ...) to
  broaden/narrow a codeset you already added. Descendants included by default. A codeset covering 0 — or
  implausibly few — patients is almost always the wrong granularity; fix it.

STRUCTURE (OHDSI cohort rules):
- start_cohort FIRST.
- add_primary_criteria for the ENTRY (index) event; pick occurrence_type matching the domain
  (Condition->ConditionOccurrence, Drug->DrugExposure, Procedure->ProcedureOccurrence,
  Measurement->Measurement, Observation->Observation, Visit->VisitOccurrence, Device->DeviceExposure).
- *** ENTRY EVENTS ARE OR'd. *** If indexed on 'A OR B OR C', call add_primary_criteria once per
  alternative; each alternative usually needs its own concept set. A single domain anchors the index date.
- Inclusion rules (add_demographic_rule / add_event_rule) are GLOBAL and AND'd — each REQUIRES that
  criterion of EVERY subject. Use ONLY for restrictions the description explicitly states (age>=18, a
  required concurrent event, an exclusion). When unsure, OMIT — most phenotype cohorts need ZERO rules.
- PER-ARM requirements (a restriction on only one entry arm) -> add_arm_correlated_criteria with that
  arm's entry_event_index, NOT a global rule.
- LAB/MEASUREMENT thresholds (e.g. 'HbA1c >= 6.5') -> value_op + value (+ value_extent for a range) on
  the Measurement/Observation entry event or rule.
- EXCLUSIONS / 'no history of X' -> add_event_rule count_op='exactly' count=0 over the lookback
  ('anytime prior' = a large negative start_days, e.g. -99999).
- WASHOUT / new users / 'first ever' -> prior_days on add_primary_criteria.
Keep it faithful and minimal. Call finalize LAST, then stop.
"""

CRITIC_PROMPT = """You are an OMOP cohort VALIDATOR reviewing a colleague's draft cohort against the
clinical description, on a MIMIC ICU OMOP database (~65k patients). Judge medical face validity and
either ACCEPT or give ONE concrete fix. Work autonomously; finish by calling critic_verdict.

Be EFFICIENT and ALWAYS finish by calling critic_verdict. Change a cohort ONLY when one of the two specific
triggers below fires; otherwise ACCEPT it unchanged.

TRIGGER 1 — collapsed by a rule. Call attrition_estimate; if an inclusion rule drops the cohort >50% (or to
~0) and is not clearly required by the description, relax_inclusion_rule(name).

TRIGGER 2 — a phenotype on this SHORT WHITELIST of common high-prevalence ICU diseases, wearing a droppable
QUALIFIER, yielding too few patients. The disease MUST be one of exactly these: heart failure; stroke /
cerebral infarction; pneumonia; sepsis; myocardial infarction; diabetes; acute kidney injury / renal
failure; COPD; venous thromboembolism (DVT/PE); atrial fibrillation; hypertension; asthma. ONLY for one of
these, if its NAME carries a laterality ('left'/'right'), acuity ('acute'/'chronic'), or mechanism
('ischemic') qualifier AND the cohort is implausibly small in a ~65k ICU DB, DROP the qualifier with
replace_concept_set_by_ids to the base same-disease family (or assemble its on-disease siblings, e.g.
ischemic-stroke mechanisms cerebral infarction/thrombosis/embolism). Same disease only.

OTHERWISE ACCEPT — this is the default and applies to the large majority of cohorts. If the phenotype is NOT
on the whitelist above, do NOT broaden it, no matter how small it looks: a specific cancer/tumor/lymphoma/
leukemia subtype, an eponymous or rare syndrome (e.g. 'overlap syndrome'), a specific anemia (e.g.
sickle-cell), organism, site, or procedure all have a CORRECT small size. Also accept any cohort already at a
plausible size, or a base disease with no qualifier to drop. When in doubt, ACCEPT (over-broadening is worse
than a slightly narrow cohort). An implausibly LARGE cohort -> narrow it. Finish with critic_verdict.
"""


def _describe_database(prompt: str) -> str:
    """Substitute the configured database description (settings.cdm_description /
    cdm_short_description). With the defaults the prompts are unchanged."""
    return (prompt.replace("a MIMIC ICU OMOP database (~65k patients)", settings.cdm_description)
                  .replace("a ~65k ICU DB", settings.cdm_short_description))


BUILDER_PROMPT = _describe_database(BUILDER_PROMPT)
CRITIC_PROMPT = _describe_database(CRITIC_PROMPT)


class MultiAgentArchitect:
    def __init__(self, resolver: Optional[ConceptResolver] = None, executor=None,
                 cdm=None, max_iters: int = 16):
        self.client = settings.make_client()
        if settings.openai_key or settings.llm_api == "ollama":
            self.builder_model = settings.openai_model_builder or settings.openai_model
            self.critic_model = settings.openai_model_critic or settings.openai_model
        else:
            self.builder_model = settings.azure_deployment_builder or settings.azure_deployment
            self.critic_model = settings.azure_deployment_critic or settings.azure_deployment
        self.resolver = resolver or ConceptResolver()
        self.executor = executor
        self.cdm = cdm if cdm is not None else getattr(self.resolver, "_cdm", None)

    # ── one shared tool-calling loop ────────────────────────────────────────
    def _dispatch(self, toolset: CohortToolset, agent: AgentToolset,
                  name: str, args: Dict[str, Any]) -> str:
        try:
            if name in AGENT_TOOLS:
                return getattr(agent, name)(**args)
            if name in _CORE_DISPATCH and hasattr(toolset, name):
                return getattr(toolset, name)(**args)
            if name == "critic_verdict":
                return json.dumps({"ok": True})
            return json.dumps({"error": f"unknown tool {name}"})
        except Exception as exc:  # a bad tool call should inform the model, not crash the run
            return json.dumps({"error": f"{name} failed: {exc}"})

    def _create_with_retry(self, model: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        return create_with_retry(self.client, model=model, messages=messages, tools=tools,
                                 tool_choice="auto", temperature=0, extra_body=settings.llm_extra_body)

    def _loop(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], model: str,
              toolset: CohortToolset, agent: AgentToolset, transcript: List[Dict[str, Any]],
              counter: List[int], max_iters: int, stop_tools: set) -> Optional[Dict[str, Any]]:
        verdict = None
        for _ in range(max_iters):
            resp = self._create_with_retry(model, messages, tools)
            msg = resp.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if settings.llm_trace:
                names = [tc.function.name for tc in (msg.tool_calls or [])]
                u = resp.usage
                print(f"      turn {counter[0]+1:>2} {model}: {names or 'text'}"
                      f"  ({u.prompt_tokens if u else '?'} tok in, {u.completion_tokens if u else '?'} tok out)",
                      flush=True)
            if msg.content:
                transcript.append({"step": "assistant", "content": msg.content})
            if not msg.tool_calls:
                break
            stop = False
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "critic_verdict":
                    verdict = args
                result = self._dispatch(toolset, agent, name, args)
                counter[0] += 1
                try:
                    parsed = json.loads(result)
                except json.JSONDecodeError:
                    parsed = result
                transcript.append({"step": "tool", "name": name, "arguments": args, "result": parsed})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                if name in stop_tools:
                    stop = True
            if stop:
                break
        return verdict

    def _cohort_state(self, builder) -> Dict[str, Any]:
        css = []
        for cs in builder._concept_sets:
            items = cs["expression"]["items"]
            css.append({
                "codeset_id": cs["id"], "name": cs["name"],
                "concept_ids": [it["concept"]["CONCEPT_ID"] for it in items if not it.get("isExcluded")],
                "excluded_ids": [it["concept"]["CONCEPT_ID"] for it in items if it.get("isExcluded")],
            })
        arms = []
        for i, p in enumerate(builder._primaries):
            occ = next(iter(p))
            arms.append({"arm": i + 1, "occurrence_type": occ, "codeset_id": p[occ].get("CodesetId")})
        return {"concept_sets": css, "entry_arms": arms,
                "inclusion_rules": [r.get("name") for r in builder._inclusion_rules]}

    # ── orchestration ───────────────────────────────────────────────────────
    def generate(self, description: str) -> Dict[str, Any]:
        toolset = CohortToolset(self.resolver, executor=self.executor)
        hierarchy = HierarchyResource(self.cdm, self.resolver, executor=self.executor)
        agent = AgentToolset(toolset, hierarchy, self.resolver)
        transcript: List[Dict[str, Any]] = []
        counter = [0]
        t0 = time.time()
        error: Optional[str] = None
        rounds = 0
        builder_tools = tool_schemas() + builder_extra_schemas()
        critic_tools = critic_schemas() + [_CRITIC_VERDICT_SCHEMA]

        try:
            builder_msgs: List[Dict[str, Any]] = [
                {"role": "system", "content": BUILDER_PROMPT},
                {"role": "user", "content": f"Clinical description:\n{description}"},
            ]
            self._loop(builder_msgs, builder_tools, self.builder_model, toolset, agent,
                       transcript, counter, MAX_BUILDER_ITERS, stop_tools={"finalize"})

            for rnd in range(MAX_ROUNDS):
                if toolset.builder is None or not toolset.builder.is_ready():
                    break
                rounds = rnd + 1
                state = self._cohort_state(toolset.builder)
                critic_msgs = [
                    {"role": "system", "content": CRITIC_PROMPT},
                    {"role": "user", "content":
                        f"Clinical description:\n{description}\n\nDraft cohort:\n"
                        f"{json.dumps(state)}\n\nReview it: call attrition_estimate, inspect "
                        f"granularity, then critic_verdict."},
                ]
                verdict = self._loop(critic_msgs, critic_tools, self.critic_model, toolset, agent,
                                     transcript, counter, MAX_CRITIC_ITERS,
                                     stop_tools={"critic_verdict"})
                transcript.append({"step": "verdict", "round": rounds, "verdict": verdict})
                if not verdict or verdict.get("accept") or not verdict.get("instruction_to_builder"):
                    break
                # hand the concrete fix back to the SAME builder conversation (keeps context)
                builder_msgs.append({"role": "user", "content":
                    f"A reviewer flagged the cohort: {verdict.get('reason')}\n"
                    f"ACTION ({verdict.get('suggested_action')}): {verdict.get('instruction_to_builder')}\n"
                    f"Apply this fix (re-pick concept granularity via the hierarchy tools if needed), "
                    f"then call finalize."})
                self._loop(builder_msgs, builder_tools, self.builder_model, toolset, agent,
                           transcript, counter, MAX_REFINE_ITERS, stop_tools={"finalize"})
        except Exception as exc:
            error = str(exc)

        circe = None
        if toolset.builder is not None and toolset.builder.is_ready():
            circe = toolset.builder.to_circe()
        relaxed = [s["arguments"].get("name") for s in transcript
                   if s.get("step") == "tool" and s.get("name") == "relax_inclusion_rule"
                   and isinstance(s.get("result"), dict) and s["result"].get("ok")]
        return {
            "ok": circe is not None,
            "circe": circe,
            "tool_calls": counter[0],
            "seconds": round(time.time() - t0, 1),
            "error": error or (None if circe else "architect did not produce a complete cohort"),
            "transcript": transcript,
            "guard": {"critic_relaxed_rules": relaxed} if relaxed else None,
            "rounds": rounds,
        }


_CRITIC_VERDICT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "critic_verdict",
        "description": "Record your final judgment and STOP. accept=true if the cohort is medically "
                       "valid and not collapsed; otherwise give a concrete instruction_to_builder.",
        "parameters": {"type": "object", "properties": {
            "accept": {"type": "boolean"},
            "reason": {"type": "string"},
            "suggested_action": {"type": "string", "enum": ["broaden", "narrow", "relax_rule", "none"]},
            "instruction_to_builder": {"type": "string"},
            "target_codeset_id": {"type": "integer"},
        }, "required": ["accept", "reason"]},
    },
}
