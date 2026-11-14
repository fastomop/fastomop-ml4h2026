# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository:
<https://github.com/fastomop/fastomop-ml4h2026/security/advisories/new>. Do not file a
public issue for security bugs. If you cannot use that channel, open an issue with a
stub title and no technical details, asking a maintainer to get in touch.

Please include the affected component, the version, a minimal reproducer, and the
impact you observed.

## Scope notes

- The framework sends SQL to an OMOP CDM database through the `omcp` server. Run that
  server with a **read-only** database connection; the governance screen and the
  validation gate in this repository are additional controls, not a replacement for it.
- Never commit `.env`, `config.local.toml`, evaluation datasets, or evaluation output.
  They can contain credentials or data derived from a restricted-access database (see
  `DATA_ACCESS.md`).

## Supported versions

Only the latest `0.x` release receives security fixes.
