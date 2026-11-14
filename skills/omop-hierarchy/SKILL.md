---
name: omop-hierarchy
description: Concept-hierarchy expansion CTEs (source -> standard -> descendants) for drugs, conditions, and observations.
---

## Always use concept hierarchies

**NEVER use single concept IDs directly.** Always expand a seed concept via
`concept_relationship` ("Maps to") and `concept_ancestor` to include all formulations,
strengths, and descendant concepts. Pattern: **source → mapped/standard → descendants**.

### Drug hierarchy (RxNorm) — required for all drug queries

```sql
WITH drug{N}_source AS (
  SELECT concept_id FROM {vocab_schema}.concept
  WHERE vocabulary_id='RxNorm' AND concept_code='{code}' AND invalid_reason IS NULL
),
drug{N}_mapped AS (
  SELECT concept_id_2 AS concept_id FROM drug{N}_source ds
  JOIN {vocab_schema}.concept_relationship cr ON ds.concept_id=cr.concept_id_1
  WHERE cr.relationship_id='Maps to'
),
drug{N}_concepts AS (
  SELECT DISTINCT ca.descendant_concept_id AS concept_id
  FROM drug{N}_mapped dm
  JOIN {vocab_schema}.concept c ON dm.concept_id=c.concept_id
  JOIN {vocab_schema}.concept_ancestor ca ON c.concept_id=ca.ancestor_concept_id
)
```

### Condition hierarchy (SNOMED) — required for all condition queries

```sql
WITH seed_{N} AS (
  SELECT concept_id AS src_id FROM {vocab_schema}.concept
  WHERE vocabulary_id='SNOMED' AND concept_code='{code}' AND invalid_reason IS NULL
),
std_{N} AS (
  SELECT DISTINCT COALESCE(cr.concept_id_2, s.src_id) AS standard_id
  FROM seed_{N} s
  LEFT JOIN {vocab_schema}.concept_relationship cr ON cr.concept_id_1=s.src_id
    AND cr.relationship_id='Maps to' AND cr.invalid_reason IS NULL
),
desc_{N} AS (
  SELECT DISTINCT ca.descendant_concept_id AS concept_id
  FROM std_{N} sa
  JOIN {vocab_schema}.concept_ancestor ca ON ca.ancestor_concept_id=sa.standard_id
  JOIN {vocab_schema}.concept c ON c.concept_id=ca.descendant_concept_id
  WHERE c.standard_concept='S' AND c.domain_id='Condition' AND c.invalid_reason IS NULL
)
```

### Observation / Measurement hierarchy (LOINC / SNOMED)

```sql
WITH obs_{N}_source AS (
  SELECT concept_id FROM {vocab_schema}.concept
  WHERE concept_code='{code}' AND invalid_reason IS NULL
),
obs_{N}_concepts AS (
  SELECT DISTINCT ca.descendant_concept_id AS concept_id
  FROM obs_{N}_source os
  JOIN {vocab_schema}.concept_ancestor ca ON os.concept_id=ca.ancestor_concept_id
  JOIN {vocab_schema}.concept c ON c.concept_id=ca.descendant_concept_id
  WHERE c.domain_id IN ('Measurement','Observation') AND c.invalid_reason IS NULL
)
```

Substitute `{N}` with the entity_order, `{code}` with the entity's `concept_code`, and
`{vocab_schema}` with the schema that holds the vocabulary tables (the one containing
`concept_ancestor` — see the discovery step in the `database-role` skill). All tables in
these CTEs are vocabulary tables, so they all use `{vocab_schema}`.
