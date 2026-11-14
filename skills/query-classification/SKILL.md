---
name: query-classification
description: Decision tree + reference table that classifies a query into one category. MUST stay in lockstep with the omop-best-practices skill.
---

## Query classification (CRITICAL)

Classify every query into exactly ONE category. The database agent uses this category to
select the correct SQL template, so the category names here MUST match those in the
`omop-best-practices` skill exactly.

### Classification rules (evaluate in order)

```
1. No clinical entities (drugs/conditions/observations/procedures)?
   ├─ "born in year YYYY" → BIRTH_YEAR
   ├─ "group by" / "grouped by" demographics → DEMOGRAPHIC
   └─ patient count only → DEMOGRAPHIC

2. Single entity?
   ├─ + "at age" filter → ENTITY_AT_AGE
   ├─ + "in year YYYY" (drug) → DRUG_IN_YEAR
   ├─ + "in year YYYY" (condition) → CONDITION_IN_YEAR
   ├─ + "grouped by year" → ENTITY_GROUP_BY_YEAR
   ├─ condition only → SINGLE_CONDITION
   ├─ drug only → SINGLE_DRUG
   ├─ observation → SINGLE_OBSERVATION
   └─ procedure only → SINGLE_PROCEDURE

3. Multiple entities with "or"?
   ├─ conditions → CONDITION_OR
   ├─ drugs → DRUG_OR
   ├─ observations → OBSERVATION_OR
   ├─ procedures → PROCEDURE_OR
   └─ mixed domains with OR logic → MULTI_ENTITY_OR

4. Drug + Condition combination?
   ├─ "drug after condition" (no days) → DRUG_AFTER_CONDITION
   └─ "drug more than N days after condition" → DRUG_DAYS_AFTER_CONDITION

5. Same-domain: Drug + Drug?
   ├─ "within N days" → DRUG_WITHIN_DAYS
   ├─ "followed by" → DRUG_FOLLOWED_BY
   ├─ "more than N days after" → DRUG_DAYS_AFTER
   └─ AND logic, no temporal → DRUG_AND

6. Same-domain: Condition + Condition?
   ├─ "within N days" → CONDITION_WITHIN_DAYS
   ├─ "followed by" → CONDITION_FOLLOWED_BY
   ├─ "more than N days after" → CONDITION_DAYS_AFTER
   └─ AND logic, no temporal → CONDITION_AND
```

### Category reference

| Category | Example query |
|----------|---------------|
| DEMOGRAPHIC | "Number of patients grouped by gender" |
| BIRTH_YEAR | "Patients born in year 2078" |
| SINGLE_DRUG | "Patients taking metformin" |
| SINGLE_CONDITION | "Patients with diabetes" |
| SINGLE_OBSERVATION | "Patients with elevated NT-proBNP" |
| SINGLE_PROCEDURE | "Patients who had an appendectomy" |
| ENTITY_AT_AGE | "Patients with condition X at age 65" |
| DRUG_IN_YEAR | "Patients taking drug X in year 2113" |
| CONDITION_IN_YEAR | "Patients with condition X in year 2145" |
| ENTITY_GROUP_BY_YEAR | "Patients with condition X grouped by year" |
| DRUG_AND | "Patients taking drug A AND drug B" |
| CONDITION_AND | "Patients with condition A AND condition B" |
| DRUG_OR | "Patients taking drug A OR drug B" |
| CONDITION_OR | "Patients with condition A OR condition B" |
| OBSERVATION_OR | "Patients with LVEF <50% OR E/e' >9" |
| MULTI_ENTITY_OR | "Patients with condition A OR observation B" |
| DRUG_WITHIN_DAYS | "Patients taking A and B within 30 days" |
| CONDITION_WITHIN_DAYS | "Patients with A and B within 30 days" |
| DRUG_FOLLOWED_BY | "Drug A followed by drug B" |
| CONDITION_FOLLOWED_BY | "Condition A followed by condition B" |
| DRUG_DAYS_AFTER | "Drug A more than 30 days after drug B" |
| CONDITION_DAYS_AFTER | "Condition A more than 30 days after condition B" |
| DRUG_AFTER_CONDITION | "Drug X after being diagnosed with condition Y" |
| DRUG_DAYS_AFTER_CONDITION | "Drug X more than 30 days after condition Y" |
