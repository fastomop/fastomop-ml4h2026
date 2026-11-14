---
name: schema-knowledge
description: Core OMOP CDM tables and columns used by the NL->SQL flow (vocabulary + clinical-event tables).
---

## OMOP CDM tables (core subset)

Tables live in **two schemas that may differ** — discover both (see the `database-role`
workflow): clinical tables under `{cdm_schema}` (the schema containing `person`) and
vocabulary tables under `{vocab_schema}` (the schema containing `concept_ancestor`). On
single-schema deployments they resolve to the same value; never assume that.

### Vocabulary tables (for hierarchy expansion) — use `{vocab_schema}`

- **concept** — `concept_id`, `concept_name`, `domain_id`, `vocabulary_id`, `concept_class_id`,
  `standard_concept` (`'S'` = standard), `concept_code`, `invalid_reason`.
- **concept_relationship** — `concept_id_1`, `concept_id_2`, `relationship_id`
  (use `'Maps to'` to reach the standard concept), `invalid_reason`.
- **concept_ancestor** — `ancestor_concept_id`, `descendant_concept_id` (expand to all descendants).

### Clinical event tables — use `{cdm_schema}`

- **person** — `person_id`, `year_of_birth`, `gender_concept_id`, `race_concept_id`,
  `ethnicity_concept_id`, `location_id`. Use `year_of_birth` for birth-year filters.
- **location** — `location_id`, `state` (+ city, zip). Join `person.location_id =
  location.location_id` for residence state/location queries.
- **drug_exposure** — `person_id`, `drug_concept_id`, `drug_exposure_start_date`, `drug_exposure_end_date`.
- **condition_occurrence** — `person_id`, `condition_concept_id`, `condition_start_date`.
- **measurement** — `person_id`, `measurement_concept_id`, `measurement_date`,
  `value_as_number`, `value_as_concept_id`. Holds both Measurement and Observation domains here.
- **procedure_occurrence** — `person_id`, `procedure_concept_id`, `procedure_date`.

### Demographic labels (gender / race / ethnicity)

To turn a `*_concept_id` on `person` into a readable label, `LEFT JOIN` a lookup of
`SELECT concept_id, concept_name FROM {vocab_schema}.concept WHERE domain_id='<Gender|Race|Ethnicity>'
AND standard_concept='S'`, and `COALESCE` the unmatched name to `'Unknown'`. Group-by queries
output the **name** (never the raw `*_concept_id`). See the `omop-best-practices` demographic section.

### Notes

- Patient counts are always `COUNT(DISTINCT person_id)`.
- A standard concept on an event table's `*_concept_id` is what clinical rows reference, so
  hierarchy CTEs must resolve to standard descendant concept_ids before joining.
