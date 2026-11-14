"""Ablation ladder for the cohort-construction task. Every arm uses the same model, the same
100 cohorts, the same circe-be executor and the same scoring; only the tooling changes.

  * ``model``   — prompt + description, ONE call, no tools. The model writes the CIRCE JSON
                  itself, concept ids from memory. (Expected to mostly fail to execute — that
                  is the measurement.)
  * ``vocab``   — + the ``lookup_concept`` tool (concept-omcp semantic search with CDM coverage).
                  Tool loop; the model still writes the CIRCE JSON itself, now with looked-up ids.
  * ``builder`` — + the 7 CIRCE builder tools (start_cohort, add_concept_set, add_primary_criteria,
                  add_event_rule, add_demographic_rule, add_arm_correlated_criteria, finalize):
                  the builder guarantees well-formed CIRCE. No hierarchy walk, no coverage /
                  attrition feedback, no Critic, no refinement round.
  * ``full``    — generate/agents.py: builder + hierarchy tools + Critic + refinement rounds.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from ..settings import settings
from ..resources.concepts import ConceptResolver
from .circe_model import CohortBuilder, ConceptItem, OCCURRENCE_TYPES
from .tools import CohortToolset, tool_schemas
from .llm_retry import create_with_retry

CIRCE_PROMPT = """You are an OMOP CDM expert. Turn the clinical cohort description into a cohort
specification. Output ONE compact JSON object and nothing else (no prose, no code fences):

{
  "name": "<short cohort name>",
  "concept_sets": [
    {"name": "<label>", "domain": "Condition|Drug|Procedure|Measurement|Observation|Visit|Device",
     "concepts": [{"id": <OMOP standard concept_id>, "name": "<concept name>", "descendants": true, "exclude": false}]}
  ],
  "entry_events": [{"concept_set": "<label>", "occurrence_type": "ConditionOccurrence|DrugExposure|ProcedureOccurrence|Measurement|Observation|VisitOccurrence|DeviceExposure"}],
  "limit": "First|All",
  "inclusion_rules": [{"name": "<label>", "concept_set": "<label>", "occurrence_type": "<as above>",
                       "start_days": -365, "end_days": 0, "count_op": "at_least|at_most|exactly", "count": 1}],
  "demographics": {"age_min": null, "age_max": null, "gender": "male|female|null"}
}

Entry events are OR-ed and define the index date; inclusion rules are AND-ed; days are relative to
the index date (negative = before). Use "First" for first/earliest-event cohorts, "All" for all
events. Concept ids must be OMOP STANDARD concept ids (SNOMED for conditions, RxNorm for drugs,
LOINC for measurements). Keep the JSON short."""

VOCAB_ADDENDUM = """

You have a tool, lookup_concept(name, domain), that searches the OMOP vocabulary and returns
candidate STANDARD concepts with their concept_id and how many patients in the database carry
that concept (with descendants). Use it to obtain every concept id you put into the JSON — do
not invent ids. When you have all ids, reply with the final JSON object as your message."""

BUILDER_PROMPT = """You are an OMOP CDM expert building ONE cohort definition with the tools provided.
Read the clinical description, then: call start_cohort; use lookup_concept to check how a
clinical term resolves in the vocabulary; add one concept set per clinical idea with
add_concept_set (concept names are resolved by vocabulary search — if a name does not resolve,
rephrase it); add the entry event(s) with add_primary_criteria; add inclusion rules with
add_event_rule / add_demographic_rule only where the description requires them; then call
finalize. Do not explain; just call the tools."""

_LOOKUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lookup_concept",
        "description": "Search the OMOP vocabulary for STANDARD concepts matching a clinical term; "
                       "returns candidates with concept_id, name, vocabulary and CDM patient coverage.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "clinical term"},
            "domain": {"type": "string", "enum": ["Condition", "Drug", "Procedure", "Measurement",
                                                  "Observation", "Visit", "Device"]},
            "top_k": {"type": "integer", "default": 8}},
            "required": ["name", "domain"]},
    },
}

def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _extract_json(text: str) -> Dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the model reply")
    return json.loads(m.group(0))


def _spec_to_circe(spec: Dict[str, Any], description: str) -> Dict[str, Any]:
    """Expand the compact spec into CIRCE through the same CohortBuilder the tools use. Purely
    structural: NO name resolution — the concept ids are whatever the model supplied."""
    b = CohortBuilder(name=str(spec.get("name") or "cohort"), description=description[:200])
    cs_ids: Dict[str, int] = {}
    occ_of: Dict[str, str] = {}
    for cs in spec.get("concept_sets") or []:
        items = []
        for c in cs.get("concepts") or []:
            try:
                cid = int(c.get("id"))
            except (TypeError, ValueError):
                continue
            items.append(ConceptItem.make(cid, str(c.get("name") or ""), str(cs.get("domain") or ""),
                                          excluded=bool(c.get("exclude")),
                                          include_descendants=c.get("descendants", True) is not False))
        if not items:
            continue
        k = _norm(cs.get("name"))
        cs_ids[k] = b.add_concept_set(str(cs.get("name") or f"cs{len(cs_ids)}"), items)
        occ_of[k] = next((o for o, d in _OCC_DOMAIN.items() if d == cs.get("domain")), "ConditionOccurrence")

    def lookup(label: Any) -> Optional[int]:
        k = _norm(label)
        if k in cs_ids:
            return cs_ids[k]
        return next((v for kk, v in cs_ids.items() if k and (k in kk or kk in k)), None)

    limit = spec.get("limit") if spec.get("limit") in ("First", "All", "Last") else "First"
    for ev in spec.get("entry_events") or []:
        cid = lookup(ev.get("concept_set"))
        if cid is None:
            continue
        occ = ev.get("occurrence_type") if ev.get("occurrence_type") in OCCURRENCE_TYPES \
            else occ_of.get(_norm(ev.get("concept_set")), "ConditionOccurrence")
        b.add_primary(occ, cid, limit_type=limit)
    for r in spec.get("inclusion_rules") or []:
        cid = lookup(r.get("concept_set"))
        if cid is None:
            continue
        occ = r.get("occurrence_type") if r.get("occurrence_type") in OCCURRENCE_TYPES \
            else occ_of.get(_norm(r.get("concept_set")), "ConditionOccurrence")
        b.add_event_rule(str(r.get("name") or "rule"), cid, occurrence_type=occ,
                         start_days=int(r.get("start_days", -365) or -365), end_days=int(r.get("end_days", 0) or 0),
                         count_op=r.get("count_op") if r.get("count_op") in ("at_least", "at_most", "exactly") else "at_least",
                         count=int(r.get("count", 1) or 1))
    d = spec.get("demographics") or {}
    if any(d.get(k) not in (None, "null", "") for k in ("age_min", "age_max", "gender")):
        g = {"male": [8507], "female": [8532]}.get(str(d.get("gender") or "").lower())
        b.add_demographic_rule("demographics", age_min=d.get("age_min") or None,
                               age_max=d.get("age_max") or None, gender_concept_ids=g)
    if not b.is_ready():
        raise ValueError("spec had no usable concept set / entry event")
    return b.to_circe()


_OCC_DOMAIN = {
    "ConditionOccurrence": "Condition", "DrugExposure": "Drug", "ProcedureOccurrence": "Procedure",
    "Measurement": "Measurement", "Observation": "Observation", "VisitOccurrence": "Visit",
    "DeviceExposure": "Device",
}


class BaselineArchitect:
    """Same generate() contract as MultiAgentArchitect."""

    def __init__(self, mode: str, resolver: Optional[ConceptResolver] = None, executor=None,
                 cdm=None, max_iters: int = 16):
        assert mode in ("model", "vocab", "builder"), mode
        self.mode = mode
        self.client = settings.make_client()
        self.model = settings.openai_model_builder or settings.llm_model
        self.resolver = resolver or ConceptResolver(cdm=cdm)
        self.executor = executor
        self.max_iters = max_iters

    def _lookup(self, name: str, domain: str, top_k: int = 8) -> str:
        cands = [{"concept_id": h.concept_id, "name": h.concept_name, "vocabulary": h.vocabulary_id,
                  "coverage": self.resolver.coverage(h.concept_id, domain)}
                 for h in self.resolver.search(name, domain=domain, top_k=int(top_k or 8))]
        return json.dumps({"query": name, "domain": domain, "candidates": cands})

    def _loop(self, messages, tools, dispatch, transcript, counter, stop_tool: Optional[str]) -> Optional[str]:
        """Generic tool loop. Returns the last assistant text (for the CIRCE-writing arms)."""
        last_text = None
        for _ in range(self.max_iters):
            resp = create_with_retry(
                self.client, model=self.model, messages=messages, temperature=0,
                extra_body=settings.llm_extra_body,
                **({"tools": tools, "tool_choice": "auto"} if tools else {}))
            msg = resp.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if settings.llm_trace:
                u = resp.usage
                print(f"      turn {counter[0]+1:>2} {self.model}[{self.mode}]: "
                      f"{[t.function.name for t in (msg.tool_calls or [])] or 'text'}"
                      f"  ({u.prompt_tokens if u else '?'} tok in, {u.completion_tokens if u else '?'} tok out)", flush=True)
            if msg.content:
                last_text = msg.content
                transcript.append({"step": "assistant", "content": msg.content[:6000]})
            if not msg.tool_calls:
                break
            done = False
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    res = dispatch(name, args)
                except Exception as exc:
                    res = json.dumps({"error": f"{name} failed: {exc}"})
                counter[0] += 1
                try:
                    parsed = json.loads(res)
                except json.JSONDecodeError:
                    parsed = res
                transcript.append({"step": "tool", "name": name, "arguments": args, "result": parsed})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": res})
                if name == stop_tool:
                    done = True
            if done:
                break
        return last_text

    def generate(self, description: str) -> Dict[str, Any]:
        transcript: List[Dict[str, Any]] = []
        counter = [0]
        t0 = time.time()
        error: Optional[str] = None
        circe: Optional[Dict[str, Any]] = None
        user = {"role": "user", "content": f"Clinical description:\n{description}"}
        try:
            if self.mode == "model":
                text = self._loop([{"role": "system", "content": CIRCE_PROMPT}, user], None,
                                  lambda n, a: json.dumps({"error": "no tools"}), transcript, counter, None)
                circe = _spec_to_circe(_extract_json(text or ""), description)
            elif self.mode == "vocab":
                def dispatch(n, a):
                    if n == "lookup_concept":
                        return self._lookup(a.get("name", ""), a.get("domain", "Condition"), a.get("top_k", 8))
                    return json.dumps({"error": f"unknown tool {n}"})
                text = self._loop([{"role": "system", "content": CIRCE_PROMPT + VOCAB_ADDENDUM}, user],
                                  [_LOOKUP_SCHEMA], dispatch, transcript, counter, None)
                circe = _spec_to_circe(_extract_json(text or ""), description)
            else:  # builder
                ts = CohortToolset(self.resolver, executor=self.executor)

                def dispatch(n, a):
                    if n == "lookup_concept":
                        return self._lookup(a.get("name", ""), a.get("domain", "Condition"), a.get("top_k", 8))
                    fn = getattr(ts, n, None)
                    return fn(**a) if fn else json.dumps({"error": f"unknown tool {n}"})
                self._loop([{"role": "system", "content": BUILDER_PROMPT}, user],
                           [_LOOKUP_SCHEMA] + tool_schemas(), dispatch, transcript, counter, "finalize")
                if ts.builder is not None and ts.builder.is_ready():
                    circe = ts.builder.to_circe()
        except Exception as exc:
            error = str(exc)
        return {
            "ok": circe is not None, "circe": circe, "tool_calls": counter[0],
            "seconds": round(time.time() - t0, 1),
            "error": error or (None if circe else "architect did not produce a complete cohort"),
            "transcript": transcript, "guard": None, "rounds": 0, "architect": self.mode,
        }
