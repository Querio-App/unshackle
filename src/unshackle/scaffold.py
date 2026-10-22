"""`unshackle init`: create the unshackle layout in a target repo. Never overwrites existing files."""

from __future__ import annotations

from pathlib import Path

from unshackle.target.layout import DBT_OVERRIDES, DEFAULT_PATHS

CONFIG_TOML = """\
# unshackle output config. Set source credentials in environment variables, not here.

[workspace]
name = "{name}"

# Where each kind of file lives. Change these to fit an existing repo,
# e.g. semantic = "models/semantic" inside a dbt project.
[paths]
{paths}

# The warehouse type labels the SQL dialect of generated files. Left unset, build uses
# what the BI export reports, else ANSI_SQL with a warning in migration/report.md.
[datasources.warehouse]
# type = "postgres"   # snowflake | bigquery | databricks | redshift | postgres | duckdb
"""

AGENTS_MD = """\
# {name}

This repo holds the company's analytics as code: metrics, schema, knowledge,
verified queries, notebooks, skills and permissions. If you use a coding agent
to finish the migration, have it read this file first.

## Where things live

| Folder | What it holds | Format |
| --- | --- | --- |
| `{semantic}/` | Metrics and the semantic layer | Apache Ossie YAML (`*.ossie.yaml`); MetricFlow YAML too in dbt projects |
| `{catalog}/<datasource>/<schema>/<table>.yml` | Tables, columns, descriptions | YAML, one file per table |
| `{knowledge}/` | Glossary, business rules, AI context | Markdown with frontmatter |
| `{queries}/` | Verified queries | SQL or Python with a comment header |
| `{notebooks}/` | Notebooks and dashboards | Jupyter `.ipynb` (SQL + Altair) |
| `{skills}/<name>/SKILL.md` | Reusable analysis playbooks | Agent Skills |
| `{memory}/MEMORY.md` | Running notes for agents | Markdown |
| `{policies}/` | Who can see or query what | Cedar + `row_filters.*.yml` |
| `{automations}/` | Scheduled jobs | YAML |
| `{evals}/` | Question -> expected answer tests | YAML |
| `migration/` | Written by unshackle only: raw exports, report, lock | - |

## Migrating from a BI tool

This repo was set up with unshackle. To finish or
re-run a migration, load `migration/skills/migrate/SKILL.md` and follow it.
TODOs written as `TODO(unshackle)` point at skills like `skills/translate-metrics`:
those live under `migration/skills/`, and the structure is documented in
`migration/spec/layout.md`. `unshackle todo` lists what's left and
`migration/report.md` has the full picture. After upgrading unshackle, refresh the
skills with `unshackle init --update-skills`.

## Rules for agents

1. Prefer a metric in `{semantic}/` over writing new aggregation SQL.
2. Prefer a verified query in `{queries}/` over writing a new one for the same question.
3. Treat knowledge files with `context: always` as standing guidance when working in this repo.
4. Review `{policies}/` before running queries. These files describe intended access rules; this repo does not enforce them by itself.
5. Charts are Altair (Vega-Lite). Keep notebooks saved without outputs (they can contain data).
6. Anything marked `TODO(unshackle)` was not migrated faithfully yet. Don't trust it until it's resolved.
"""

GITIGNORE = ".unshackle/\n*.html\n.env\n.ipynb_checkpoints/\n"


def init(repo: Path, name: str | None = None, update_skills: bool = False) -> list[str]:
    repo.mkdir(parents=True, exist_ok=True)
    is_dbt = (repo / "dbt_project.yml").exists()
    paths = dict(DEFAULT_PATHS)
    if is_dbt:
        paths.update(DBT_OVERRIDES)
    name = name or repo.resolve().name
    created = []

    def create(rel: str, content: str) -> None:
        p = repo / rel
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        created.append(rel)

    create(
        "unshackle.toml",
        CONFIG_TOML.format(name=name, paths="\n".join(f'{k} = "{v}"' for k, v in paths.items())),
    )
    create("AGENTS.md", AGENTS_MD.format(name=name, **paths))
    create(f"{paths['memory']}/MEMORY.md", "# Memory\n")
    for key in ("semantic", "catalog", "knowledge", "queries", "notebooks", "skills", "policies", "automations", "evals"):
        create(f"{paths[key]}/.gitkeep", "")
    from unshackle.resources import copy_skills

    skills = copy_skills(repo, update=update_skills)
    if skills:
        created.append(f"migration/skills/ and migration/spec/ ({len(skills)} files)")
    gi = repo / ".gitignore"
    if gi.exists():
        text = gi.read_text()
        if ".unshackle/" not in text:
            gi.write_text(text.rstrip() + "\n" + GITIGNORE)
            created.append(".gitignore (updated)")
    else:
        create(".gitignore", GITIGNORE)
    return created
