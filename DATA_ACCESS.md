# Data access

This repository contains code only. It ships no clinical data, no OMOP vocabulary, and
no evaluation dataset or output.

## What you need to run it

- **An OMOP CDM database** (v5.4), reachable through the
  [omcp](https://github.com/fastomop/omcp) server, with the OMOP vocabulary tables.
- **An OMOP vocabulary download** from [Athena](https://athena.ohdsi.org), from which
  [concept-omcp](https://github.com/fastomop/concept-omcp) builds its search index.
  Vocabulary licences are accepted on Athena.

## The data used in the paper

The paper's experiments used MIMIC-IV mapped to OMOP CDM v5.4 with the public
[dbt_mimic_omop](https://github.com/CogStack/dbt_mimic_omop) project. MIMIC-IV is
distributed by PhysioNet under a credentialed-access data use agreement. The database,
the reference query set built from it, and per-query evaluation records are derivatives
of that data and may not be redistributed, so they are not in this repository. The
portability run used a real-world clinical dataset from the Lancashire Teaching
Hospitals NHS Foundation Trust Secure Data Environment (IDRIL), which cannot be
redistributed either.

## Regenerating an NL2SQL reference set

1. Obtain MIMIC-IV through PhysioNet, or use your own OMOP CDM database.
2. For MIMIC-IV, map it to the OMOP CDM with `dbt_mimic_omop`, producing a DuckDB
   database with a CDM schema and a vocabulary schema.
3. Run [foem](https://github.com/fastomop/foem) against the database. It instantiates
   question and SQL templates with concepts present in the database, executes the
   reference SQL there, and writes `{id, input, expected_output, execution_result}`
   records.
4. Put the resulting file under `datasets/` (untracked) and pass it to
   `clean-fastomop-eval`.

Execution accuracy is only meaningful when `execution_result` was computed against the
same database the agent queries. After any change to the database, regenerate the
reference set.

## Reference cohorts

The cohort workflow (`cohort/`) compares generated definitions with published ones.
Reference definitions and their descriptions are public in the
[OHDSI Phenotype Library](https://github.com/OHDSI/PhenotypeLibrary); see
`cohort/README.md` for the expected layout.
