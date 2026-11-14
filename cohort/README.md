# Cohort construction

This package turns a free-text cohort description into an executable OHDSI CIRCE-JSON
definition, compiles it with [circe-be](https://github.com/OHDSI/circe-be), and executes
it on an OMOP CDM DuckDB database. It can also run against a set of reference definitions
and compare the resulting patient sets.

The model does not write CIRCE-JSON directly. It calls builder tools (concept sets,
entry events, inclusion rules, previews) that assemble a typed representation, from which
the CIRCE-JSON is emitted. Concept names are resolved through
[concept-omcp](https://github.com/fastomop/concept-omcp). A critic agent checks the
definition and can send it back for revision, for at most `CRITIC_MAX_ROUNDS` rounds.

## Requirements

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
- JDK 17, for the Circe compiler wrapper.
- An OMOP CDM DuckDB database with the vocabulary tables.
- A built concept-omcp index (see the concept-omcp README).
- An OpenAI-compatible model endpoint, or a local Ollama.

## Setup

```sh
cd cohort
uv sync --extra dev
cp .env.example .env            # then fill in the paths and the endpoint
uv run clean-fastomop-cohort build-jar
uv run clean-fastomop-cohort models   # checks the endpoint
```

## Generating one definition

```sh
uv run clean-fastomop-cohort generate "Adults with a first diagnosis of type 2 diabetes"
uv run clean-fastomop-cohort generate --file description.txt --out cohort.json --trace trace.json
uv run clean-fastomop-cohort generate --file description.txt --execute
```

The CIRCE-JSON goes to standard output, or to `--out`. `--trace` saves the tool calls
the model made, and `--execute` also compiles and runs the definition and reports the
number of patients. No reference set is needed. The definition can be imported into
ATLAS.

## Reference cohorts

A `run` reads cohort descriptions and reference definitions from the directory named by
`GOLD_ROOT`. That directory holds an `index.rich.json`, a JSON list with one entry per
cohort:

```json
{
  "id": "710",
  "name": "Cirrhosis of liver or its sequela",
  "descriptionFile": "sources/710.description.txt",
  "circeFile": "sources/710.circe.json"
}
```

`descriptionFile` is the text the model receives. `circeFile` is the reference
CIRCE-JSON, which is withheld from the model and executed for comparison. Published
definitions and descriptions are available in the
[OHDSI Phenotype Library](https://github.com/OHDSI/PhenotypeLibrary). No reference set
is shipped here.

## Running against a reference set

```sh
uv run clean-fastomop-cohort run --n 20 --out results/my_run             # 20 sampled cohorts
uv run clean-fastomop-cohort run --strata 50,30,20 --out results/my_run  # stratified by complexity
uv run clean-fastomop-cohort run --gold-only --n 5                       # execute references only
uv run clean-fastomop-cohort report --out results/my_run                 # per-cohort breakdown
uv run clean-fastomop-cohort report-md --out results/my_run              # write REPORT.md
```

`--architect` (also accepted by `generate`) selects how much of the workflow is used:

| Value | The model receives |
|---|---|
| `model` | the description only, and writes CIRCE-JSON itself |
| `vocab` | plus the terminology tool |
| `builder` | plus the typed builder tools |
| `full` (default) | plus relationship tools, previews, and critic revision rounds |

`--resume` continues an interrupted run, `retry-failed` re-generates only the cohorts
whose generation failed, and `replay` re-executes the stored definitions of a finished
run.

Each run writes `results.jsonl` (per cohort: description, generated CIRCE-JSON, the
tool-call trace, set sizes, and the comparison) and `summary.json`. Agreement is the
patient-level Jaccard similarity between the generated and reference cohorts. Results
contain counts from your database and are not tracked.

## Code map (`src/clean_fastomop_cohort/`)

| Module | Role |
|---|---|
| `cli.py` | `build-jar`, `models`, `generate`, `run`, `retry-failed`, `replay`, `report`, `report-md` |
| `experiment.py` | `generate_one` (one description); per reference cohort: generate, compile, execute both, compare |
| `settings.py` | `.env` configuration; builds the LLM client |
| `strata.py` | complexity strata from the reference CIRCE structure |
| `resources/goldset.py` | the reference set and its seeded stratified sample |
| `resources/concepts.py` | the terminology tool: name to standard concept, with CDM coverage |
| `resources/hierarchy.py` | relationship tools: descendants, ancestors, coverage and attrition previews |
| `resources/cdm.py` | one read-only DuckDB connection to the CDM |
| `generate/circe_model.py` | the typed representation: `CohortBuilder` to CIRCE-JSON |
| `generate/tools.py`, `generate/agent_tools.py` | the builder tools the model calls |
| `generate/agents.py` | the full workflow: builder and critic agents, revision rounds |
| `generate/baselines.py` | the reduced workflows `model`, `vocab`, `builder` |
| `generate/llm_retry.py`, `generate/ollama_client.py` | transient-error retry; native Ollama client |
| `execute/compiler.py`, `execute/executor.py` | circe-be compilation (JVM) and execution |
| `compare.py` | Jaccard similarity, outcome classes, aggregate metrics |
| `report_md.py` | `REPORT.md` generation |

## Using another database

The prompts describe the target database to the model, and the defaults are the wording
used for the paper's runs on MIMIC-IV ("a MIMIC ICU OMOP database (~65k patients)"). Set
`CDM_DESCRIPTION` and `CDM_SHORT_DESCRIPTION` in `.env` for another database. One rule in
the critic prompt, which broadens over-specific concept sets, lists common ICU diagnoses;
review it in `generate/agents.py` before relying on the critic outside critical care.

## Third-party components

`tools/circe-compiler/lib/` contains unmodified OHDSI and Apache jars under their own
licences; see [`../NOTICE`](../NOTICE).
