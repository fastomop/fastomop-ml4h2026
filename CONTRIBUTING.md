# Contributing to fastomop-ml4h2026

Thanks for considering a contribution.

## Quick start

1. Fork and clone the repository.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and sync the
   development environment:

   ```sh
   uv sync --frozen --extra dev
   ```

3. Make your change.
4. Run the checks before opening a pull request:

   ```sh
   uvx ruff check src/ tests/
   uv run --frozen --no-sync pytest tests/ -v
   ```

5. Update `CHANGELOG.md`.
6. Open a pull request against `main`.

The unit tests need no database, model endpoint, or network access.

## Conventions

- Agent instructions live in `skills/<name>/SKILL.md`, not in code. The category names
  in `query-classification` and `omop-best-practices` are coupled; change them together.
- Machine-specific paths and hosts belong in `config.local.toml`, secrets in `.env`.
  Neither is tracked.
- Do not add data derived from MIMIC-IV or any other restricted-access source: no
  reference datasets, query results, or evaluation records (see `DATA_ACCESS.md`).

## Licence

Contributions are accepted under the Apache-2.0 licence of this repository.
