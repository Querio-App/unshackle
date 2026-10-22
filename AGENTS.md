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
