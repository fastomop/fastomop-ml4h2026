---
name: entity-extraction
description: Rules for extracting clinical entities and selecting the right concept (combination drugs, formulation vs ingredient).
---

## Entity extraction

### Combination drugs (CRITICAL)

Drug names with ` / ` separators are SINGLE combination products, not multiple drugs:

```
"calcium chloride 0.2 MG/ML / potassium chloride 0.3 MG/ML / sodium chloride 6 MG/ML / sodium lactate 3.1 MG/ML Injectable Solution"
```

→ This is ONE drug (Lactated Ringer's). Search with the full name; do NOT split it into parts.

### Concept selection priority (CRITICAL)

When `search_concepts` returns several candidates, choose by specificity:

1. **Formulation-specific** (term includes a strength / volume / form, e.g. "metformin 500 MG
   Oral Tablet"): match the exact formulation.
2. **Ingredient-level** (just the drug name, e.g. "metformin"): select the shortest standard
   `concept_name` — the pure ingredient.

If a strength is present (e.g. "350 mg", "3.1 MG/ML"), always match the exact formulation
rather than the bare ingredient.

### Multiple entities

- Assign `entity_order` (1, 2, 3 …) in the order entities appear in the query — the database
  agent relies on this ordering for directional temporal logic ("A followed by B").
- Extract every distinct clinical entity; demographic constraints (age, gender, year) go into
  `additional_filters`, not `entities`.
