# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0]

First public release, accompanying the ML4H 2026 paper.

### Added

- NL2SQL pipeline: semantic agent (typed request context, concepts grounded through
  concept-omcp) and database agent (SQL through omcp), orchestrated in plain Python.
- Instruction modules (`skills/`) composed per agent from `config.toml`.
- Pre-generation governance screen on the semantic agent's `request_class`.
- Pre-execution SQL validation gate using FastSSV, with revision on blocked queries.
- Experimental FastSSV-feedback refinement mode (`--refine`).
- Evaluation harness (`clean-fastomop-eval`) with strict and count execution accuracy.
- Cohort construction (`cohort/`, `clean-fastomop-cohort`): cohort description to
  CIRCE-JSON through a typed builder, compiled with OHDSI circe-be and executed on an
  OMOP CDM DuckDB; `generate` for a single description, `run` against a reference set.
