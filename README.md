# fastomop-ml4h2026

Code release for the ML4H 2026 paper *Separating Reasoning from Execution: A
Tool-Mediated Agentic Framework for NL2SQL and Cohort Construction on OMOP CDM*. The
Python package is `clean_fastomop`.

A tool-mediated agentic framework for natural-language access to the OMOP Common Data
Model (CDM). It turns a clinical question into validated OMOP SQL, and a free-text cohort
description into an executable OHDSI CIRCE-JSON definition.

The design separates language-model reasoning from execution:

1. **Bounded reasoning.** Each LLM component performs one semantic task and emits a
   structured object.
2. **Mediated action.** Terminology lookup, schema access, and SQL execution go through
   tools. The model never holds a database connection.
3. **Externalised knowledge.** OMOP conventions live in Markdown instruction modules
   (`skills/`), vocabulary grounding in a search service, and query checks in a static
   validator, not in the model.

See [Citation](#citation) for how to cite the paper.

## Architecture

```
question
   |
   v
semantic agent  --(concept-omcp: search_concepts, ...)-->  SemanticContext (JSON)
   |
   v
governance screen   halts unless request_class is "answerable"
   |
   v
database agent  --(omcp: Get_Information_Schema)-->  SQL
   |
   v
validation gate (FastSSV)   blocks on ERROR findings; the agent revises
   |
   v
omcp: Select_Query (read-only)  -->  rows
```

Orchestration is plain Python (`workflow.run_pipeline`). The framework builds on three
companion projects, each in its own repository:

| Project | Role |
|---|---|
| [omcp](https://github.com/fastomop/omcp) | MCP server for schema access and SQL execution on an OMOP CDM database (`Get_Information_Schema`, `Select_Query`) |
| [concept-omcp](https://github.com/fastomop/concept-omcp) | MCP server for semantic OMOP vocabulary search (`search_concepts`, `get_concept_by_id`, `find_concepts_by_relationship`) |
| [FastSSV](https://github.com/fastomop/fastSSV) | Static semantic validator for OMOP SQL, used as the pre-execution gate |

[foem](https://github.com/fastomop/foem) generates evaluation datasets for the harness
described under [Evaluation](#evaluation).

Cohort construction lives in [`cohort/`](cohort/README.md) and uses the same terminology
service.

## Repository layout

```
config.toml                 models/providers, [agents.*] with skill lists, [mcp.*] servers, gates
config.local.toml.example   template for machine-specific overrides (paths, hosts)
skills/<name>/SKILL.md      composable instruction modules
src/clean_fastomop/
  config.py                 configuration loading and provider/skill resolution
  models.py                 create_model(): provider config -> agno model instance
  skills.py                 load_skill / compose_instructions
  integrations.py           connect_omcp / connect_concept_omcp -> agno MCPTools
  agents.py                 semantic agent, database agent
  governance.py             pre-generation screen on the semantic agent's request_class
  validation.py             FastSSV gate on Select_Query
  refinement.py             experimental FastSSV-feedback refinement loop
  workflow.py               run_pipeline (semantic -> screen -> database)
  eval.py                   dataset evaluation: capture, compare, report
  run.py                    command-line interface
tests/                      unit tests (no database, model, or network needed)
examples/example_gold.json  ten synthetic records showing the evaluation dataset format
cohort/                     cohort description -> CIRCE-JSON -> execution
```

## Requirements

- Python 3.10 to 3.12 and [uv](https://docs.astral.sh/uv/).
- An OMOP CDM v5.4 database with the OMOP vocabulary tables.
- A checkout of [omcp](https://github.com/fastomop/omcp), configured against that
  database with a read-only connection.
- A checkout of [concept-omcp](https://github.com/fastomop/concept-omcp) with a built
  index (`index.turbovec`, `concepts.db`). Follow its README to build the index from an
  [Athena](https://athena.ohdsi.org) vocabulary download.
- A model: a local [Ollama](https://ollama.com) model, any OpenAI-compatible endpoint, or
  an Anthropic, OpenAI, or Azure OpenAI account.

## Installation

```sh
git clone https://github.com/fastomop/fastomop-ml4h2026
cd fastomop-ml4h2026
uv sync --extra dev
uv run pytest
```

FastSSV is installed from its repository at a pinned commit.

## Configuration

Configuration is layered. `config.toml` holds portable defaults, `config.local.toml`
holds machine-specific values and overrides `config.toml` by deep merge, and `.env`
supplies secrets.

1. Copy the templates:

   ```sh
   cp config.local.toml.example config.local.toml
   cp .env.example .env
   ```

2. In `config.local.toml`, set the paths to the two servers: `[mcp.omcp].command` and
   `cwd`, and `[mcp.concept_omcp].args` and `artifact_dir`.

3. Choose a model provider per agent with `model_provider` in `[agents.*]`. The default
   is `ollama_gemma4_12b` (Gemma 4 12B through a local Ollama). To use an
   OpenAI-compatible endpoint, set `base_url` and `model_id` under
   `[models.providers.openai_compatible]` and the key in `.env`. Changing providers
   needs no code change.

4. `[validation]` and `[governance]` in `config.toml` switch the validation gate and the
   governance screen. Both are on by default.

## Usage

Run one question through the full pipeline:

```sh
uv run clean-fastomop "How many patients take metformin?"
```

This prints the SemanticContext JSON, the SQL passed to `Select_Query`, the returned
rows, and the final answer.

Run only the semantic stage (uses concept-omcp; does not start omcp or execute SQL):

```sh
uv run clean-fastomop --semantic-only "type 2 diabetes mellitus"
```

`--refine` runs an experimental mode in which a template-free database agent is steered
by FastSSV feedback.

## Evaluation

`clean-fastomop-eval` runs a dataset of records
`{id, input, expected_output, execution_result}` through the pipeline. For each record
it captures the SQL submitted to `Select_Query` and the rows returned, and compares the
rows with `execution_result`. `examples/example_gold.json` shows the format with
synthetic values.

```sh
uv run clean-fastomop-eval path/to/gold.json --limit 10                  # first 10 records
uv run clean-fastomop-eval path/to/gold.json --sample 50 --concurrency 2 # random sample
uv run clean-fastomop-eval path/to/gold.json --ids 1,2,3                 # specific records
```

`--resume` continues an interrupted run into the same `--out` directory, and
`--report-only <records.jsonl>` rebuilds the report without re-running the pipeline.
`--ab BASELINE TREATMENT` compares two runs on paired records.

- **Strict execution accuracy**: the returned rows equal the reference rows. The
  comparison is order-insensitive across and within rows and coerces numeric and string
  types.
- **Count accuracy**: a secondary metric that accepts a result whose counts match the
  reference even when grouping labels differ.

Execution accuracy is only meaningful if `execution_result` was computed against the
same database the agent queries. Generate the dataset with
[foem](https://github.com/fastomop/foem) on your database, and regenerate it after any
change to the database.

Each run writes `records.jsonl`, `report.json`, and `report.md` to
`eval_runs/<timestamp>/`. These files contain query results from your database and are
not tracked.

## Instruction modules

Agent instructions are composed at build time from the modules listed per agent in
`config.toml`:

- semantic agent: `semantic-role`, `request-triage`, `entity-extraction`,
  `query-classification`, `temporal-extraction`
- database agent: `database-role`, `omop-hierarchy`, `omop-best-practices`,
  `schema-knowledge`

The category names in `query-classification` and `omop-best-practices` are coupled and
must be kept consistent.

## Data availability

This repository contains code only. The paper's evaluation used MIMIC-IV mapped to OMOP,
which is available from PhysioNet under a credentialed data use agreement, and a
real-world NHS dataset that cannot be redistributed. See
[DATA_ACCESS.md](DATA_ACCESS.md) for what you need to run the framework and how to
regenerate an evaluation dataset.

## Citation

```bibtex
@inproceedings{moellergrell2026separating,
  title     = {Separating Reasoning from Execution: A Tool-Mediated Agentic Framework
               for {NL2SQL} and Cohort Construction on {OMOP} {CDM}},
  author    = {M{\"o}ller-Grell, Niko and Shenzhang, Shihao and Jiang, Zhangshu Joshua
               and Dobson, Richard JB and Chandrabalan, Vishnu V},
  booktitle = {Machine Learning for Health (ML4H) 2026},
  series    = {Proceedings of Machine Learning Research},
  volume    = {346},
  year      = {2026}
}
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
