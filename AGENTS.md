# Agent instructions

This repo is `unshackle`: it migrates BI tools into open formats in a Git repo.
There are two ways you might be here.

## Migrating a customer's BI tool

You're running inside the customer's repo with `unshackle` installed. Load
`skills/migrate/SKILL.md` and follow it. The other skills:

| Skill | Use it for |
| --- | --- |
| `skills/source-<tool>/SKILL.md` | Access, what's automatic, tool-specific translation (looker, lightdash, omni, hex, sigma, thoughtspot, powerbi, tableau, metabase, superset, redash) |
| `skills/translate-metrics/SKILL.md` | Calculations -> Apache Ossie (plus MetricFlow in dbt projects) or SQL models |
| `skills/translate-dashboards/SKILL.md` | Finishing notebook tiles and Altair charts |
| `skills/translate-permissions/SKILL.md` | BI security -> Cedar + row filters |
| `skills/verify/SKILL.md` | Parity checks against the source tool |

Rules: credentials stay in the environment, never in files. Work on a branch and
open a pull request. Never delete a `TODO(unshackle)` without resolving it.

## Working on unshackle itself

- Code: `src/unshackle/`. `ir.py` is the neutral model; `sources/<tool>.py` implement
  `inventory`, `extract`, `parse` (see `sources/base.py`); `target/` writes the structure.
- `parse()` never touches the network and is tested from `tests/fixtures/<tool>/`.
- Anything a parser can't translate faithfully keeps `source_expression` and sets `todo`.
- Generated SQL divides with `1.0 * a / nullif(b, 0)`. Dashboards are Jupyter
  notebooks (`target/notebooks.py`) with Altair charts; facets, layers and concatenation
  are fine when the source chart needs them.
- Run `uv pip install -e ".[dev]" && pytest` and `./examples/tableau-demo/run.sh`
  before pushing.
- Live end-to-end tests are in `tests/live/` (skipped unless `UNSHACKLE_LIVE=1`); see
  `tests/live/README.md`. Self-hosted tools run in Docker; hosted tools need their own
  credentials in the environment.
- Fixtures and test data never contain real hostnames, site or tenant names, account ids,
  emails or tokens. Replace them with `example.*` values before committing.

### Workflow

- Work is tracked in GitHub issues. One branch and one pull request per change; say which
  issue it closes (`Closes #N`).
- Sign off every commit (`git commit -s`; see `CONTRIBUTING.md`).
- PR descriptions are plain and factual: what changed, how it was tested, what stays TODO.
  Pull requests and issues written with a coding agent end with the line
  `Assisted by Claude Code` (or the equivalent for your agent).
- Changes that touch the same files as an open PR (`CHANGELOG.md`, `pyproject.toml` extras,
  `tests/live/README.md`) are rebased on `main` after that PR merges.
