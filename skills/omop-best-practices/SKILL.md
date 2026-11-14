---
name: omop-best-practices
description: How to construct an OMOP CDM cohort query — the decisions the query class and entity domains determine, and why.
---

## What this module is for

You are turning a resolved semantic representation into one SQL statement over an OMOP CDM
instance. The representation already tells you *what* is being asked: the entities with their
resolved concepts, the query class, any temporal relation, and any filters. This module tells
you *how* those facts determine the shape of the query, and why each decision is what it is.

Construct the query; do not pattern-match one. Every rule below follows from a property of the
CDM or of the question, and if you can name the property you can handle a phrasing that none
of the worked cases covers.

## The invariant: what a cohort query counts

Almost every question here asks **how many distinct people** satisfy a condition, so the
result is `COUNT(DISTINCT person_id)`. The CDM records *events*, not people: one patient may
have fifty exposures to the same drug. Counting rows counts prescriptions; counting distinct
`person_id` counts patients. When a breakdown is requested the same invariant holds within
each group.

## Step 1 — Domain determines the table and its date columns

Each resolved entity carries a `domain_id`. The domain fixes which clinical table holds the
event, which column carries the concept, and which columns carry its timing.

| domain | table | concept column | timing |
|---|---|---|---|
| Drug | `drug_exposure` | `drug_concept_id` | `drug_exposure_start_date`, `drug_exposure_end_date` |
| Condition | `condition_occurrence` | `condition_concept_id` | `condition_start_date` |
| Measurement | `measurement` | `measurement_concept_id` | `measurement_date` |
| Observation | `observation` | `observation_concept_id` | `observation_date` |
| Procedure | `procedure_occurrence` | `procedure_concept_id` | `procedure_date` |

Two of these behave differently in time, and it matters later:

- a **drug exposure is an interval** — it has a start and an end;
- a **condition is a point event** — it is recorded as having begun.

Clinical tables live in `{cdm_schema}`; vocabulary tables live in `{vocab_schema}`. The two
may differ — the discovery step in `database-role` resolves them. Never assume they coincide.

## Step 2 — Never match a bare concept_id

A resolved concept identifies *one* vocabulary term. Patient records are coded to many terms
beneath it. "Patients on metformin" must include every metformin product, not the single
ingredient concept the semantic step returned.

So every clinical entity is expanded before use:

1. map the resolved concept to its **standard** concept (`Maps to`), because records are coded
   to standard concepts;
2. expand to that standard concept **and all its descendants** via `concept_ancestor`;
3. match the event table's concept column against the resulting set.

The `omop-hierarchy` module supplies the CTEs that do this; `[drug hierarchy CTEs]` below
means "that module's expansion, for this entity".

Skipping the expansion is the most common way to return a large, plausible, wrong number.
Matching `concept_name LIKE '%...%'` instead is worse: it mixes vocabularies and misses
synonyms.

## Step 3 — The query class fixes the set operation

The class encodes how multiple entities combine. Translate the set logic, not the English
word.

**One entity** (`SINGLE_*`, `*_IN_YEAR`, `*_GROUP_BY_YEAR`) — expand the concept, join the
event table to the expansion, count distinct persons.

**Conjunction** (`*_AND`) — a person must satisfy *both*, in any order, at any time: set
**intersection** on `person_id`. Reduce each entity to a distinct person set first, then join:

```sql
WITH [entity_a hierarchy], [entity_b hierarchy],
persons_a AS (SELECT DISTINCT co.person_id FROM {cdm_schema}.condition_occurrence co
              JOIN desc_a ON co.condition_concept_id = desc_a.concept_id),
persons_b AS (SELECT DISTINCT co.person_id FROM {cdm_schema}.condition_occurrence co
              JOIN desc_b ON co.condition_concept_id = desc_b.concept_id)
SELECT COUNT(DISTINCT a.person_id) FROM persons_a a JOIN persons_b b USING (person_id);
```

Collapsing to person sets before joining is what keeps this stable. Joining the raw event
tables also answers a plain conjunction, but it multiplies rows, and once a temporal predicate
is added that multiplication changes which events get compared.

**Disjunction** (`*_OR`, `MULTI_ENTITY_OR`) — a person satisfying either qualifies once: set
**union**. Use `UNION`, not `UNION ALL`, so people are de-duplicated; the entities may sit in
different domains, so each arm reads its own table:

```sql
WITH [hierarchy CTEs per entity],
union_results AS (
  SELECT DISTINCT person_id FROM {cdm_schema}.condition_occurrence co
    JOIN desc_1 ON co.condition_concept_id = desc_1.concept_id
  UNION
  SELECT DISTINCT person_id FROM {cdm_schema}.measurement m
    JOIN obs_2 ON m.measurement_concept_id = obs_2.concept_id
)
SELECT COUNT(DISTINCT person_id) FROM union_results;
```

## Step 4 — Temporal relations: symmetric or directional

The temporal object carries a `type`, a `value` and a `direction`. Two questions decide the
predicate: **is the relation symmetric or directional**, and **which event is the anchor**.

**Symmetric — "within N days".** Order does not matter, only the gap. Take the span between
the latest and earliest event:

```sql
WHERE CAST(EXTRACT(epoch FROM
      CAST(GREATEST(a.start_date, b.start_date) AS TIMESTAMP) -
      CAST(LEAST (a.start_date, b.start_date) AS TIMESTAMP)) / 86400 AS BIGINT) <= {days}
```

**Three or more entities "all within N days"** means all starts fall in **one shared window**.
Join every exposure set on `person_id`, then apply a single `GREATEST`/`LEAST` across *all* the
dates. Do **not** decompose into pairwise checks: pairwise (1–2, 1–3, 2–3) over-counts, because
each pair may be satisfied by a different pair of events with no single window containing all
of them.

**Directional — "followed by", "N days after".** One event is the **anchor** (the reference
point, earlier) and one is the **outcome** (the event counted, later). Always subtract
**outcome minus anchor**, so a satisfied relation is positive:

```sql
WHERE CAST(EXTRACT(epoch FROM
      CAST(outcome.start_date AS TIMESTAMP) -
      CAST(anchor.start_date  AS TIMESTAMP)) / 86400 AS BIGINT) > {days}
```

with `> 0` for a bare "followed by" and `> {days}` for an N-day threshold.

**Assign the roles by meaning, never by order of mention.** English routinely names the outcome
first: *"treated by drug X after more than 30 days of starting with drug Y"* counts people on
**X**; **Y** is only the reference point. The semantic representation records this in
`direction` — `first_to_second` means the entity mentioned first is the anchor,
`second_to_first` means it is the outcome. Read `direction`, not the word order. Inverting the
subtraction returns a different cohort of similar size, so the error is silent.

## Step 5 — Year and age filters follow from event shape

"In year Y" means different things for an interval and a point event — this is where Step 1's
distinction pays off.

- **Drug in year Y** — the exposure was *active during* Y. It may have begun earlier and run
  into Y, so test the interval: `start_year <= Y AND end_year >= Y`.
- **Condition in year Y** — the diagnosis *began* in Y:
  `EXTRACT(year FROM condition_start_date) = Y`.
- **Grouped by year** — group on the **start** year in both domains; the question is about when
  the event began.
- **Age at event** — age is not stored. Derive it from `person.year_of_birth` and the event
  date; there is no age column to filter on.

## Step 6 — Grouped output returns labels, not identifiers

When the class asks for a breakdown (`DEMOGRAPHIC`, `*_GROUP_BY_*`), the grouping key is a
human-readable label. Three rules:

1. **Return the concept name, never the `concept_id`.** A column of integers does not answer
   "grouped by race".
2. **`LEFT JOIN` the label lookup and `COALESCE` unmapped values to `'Unknown'`.** An inner
   join silently drops every patient whose demographic is unrecorded, changing the denominator.
3. **Select only the label(s) and the count.** An extra identifier column changes the shape of
   the result.

Build the lookup by filtering the concept table to the right domain — `Gender`, `Race`,
`Ethnicity` — restricted to standard concepts, then join it from `person` on the matching
`*_concept_id`:

```sql
WITH race_c AS (SELECT concept_id, concept_name AS race FROM {vocab_schema}.concept
                WHERE domain_id = 'Race' AND standard_concept = 'S')
SELECT COALESCE(r.race, 'Unknown') AS race, COUNT(DISTINCT p.person_id)
FROM {cdm_schema}.person p
LEFT JOIN race_c r ON p.race_concept_id = r.concept_id
GROUP BY r.race;
```

One domain CTE and one `LEFT JOIN` per dimension. Two attributes need no concept join at all:
`year_of_birth` is a column on `person`, and residence state is reached by joining `location`
on `location_id`.

## Composing beyond the listed classes

The classes are not an enumeration to be matched — they name combinations of the decisions
above. A class this module does not discuss is built by composing them: `ENTITY_AT_AGE` is a
single-entity query plus a Step 5 age filter; `DRUG_AFTER_CONDITION` is a directional Step 4
relation whose two sides come from different Step 1 tables. If you can name the domain, the
set operation, the temporal relation and the output shape, you can construct the query.

## Before returning the SQL, check

- Every clinical entity expanded through `Maps to` and `concept_ancestor`.
- Counting `DISTINCT person_id`, not rows.
- Conjunction as intersection; disjunction as `UNION`.
- Directional subtraction is outcome minus anchor, roles taken from `direction`.
- Multi-entity "within N days" uses one shared window, not pairwise comparisons.
- Grouped queries return labels, `LEFT JOIN`ed and `COALESCE`d.
- Clinical tables qualified with `{cdm_schema}`, vocabulary tables with `{vocab_schema}`.
