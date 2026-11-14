---
name: temporal-extraction
description: Map temporal language in the query to the temporal_constraint object.
---

## Temporal constraint extraction

When temporal language is present, populate `temporal_constraint`:

| Language | type | direction |
|----------|------|-----------|
| "within N days" | within | symmetric |
| "after" / "followed by" | after | directional (first_to_second) |
| "more than N days after" | days_after | directional (first_to_second) |
| "before" | before | directional |
| "in year YYYY" | in_year | n/a |

- `value` is the number of days (or the year for `in_year`); `unit` is `days` or `year`.
- `direction` describes entity order: `first_to_second` means entity_order 1 precedes
  entity_order 2. Use `symmetric` for "within N days" (order does not matter).
- If there is no temporal language, set `temporal_constraint` to `null`.

### Entity order vs. order in time

`entity_order` follows the **order the entities are mentioned** in the question. It does
**not** encode chronology — `direction` does that. Keep the two separate:

| Role | Cue words | entity_order | In time |
|------|-----------|--------------|---------|
| **outcome** (the counted event) | "treated by", "took", "followed by" | **1** (named first) | **later** |
| **anchor** (the index event) | "starting with", "started on", "after ... of" | **2** (named second) | **earlier** |

> "How many people have **treated by drug X** after more than 30 days of **starting with
> drug Y**?" — X is entity_order **1** (mentioned first, and the drug counted); Y is
> entity_order **2**. In time Y comes first, so the relation runs from entity 2 to
> entity 1: set `"direction": "second_to_first"`.

For this "A after N days of starting with B" phrasing use `second_to_first`, because the
entity named first is the *later* event. Reserve `first_to_second` for phrasings where
mention order already matches chronology ("drug A followed by drug B").

The downstream SQL template pairs `entity_order` 1 → `drug1` and 2 → `drug2`, and
subtracts `drug1 - drug2`, so mislabelling the order silently inverts the cohort.

**Example:**
```json
"temporal_constraint": { "type": "days_after", "value": 30, "unit": "days", "direction": "first_to_second" }
```
