---
name: migrate
description: Use when migrating a BI tool (Looker, Lightdash, Omni, Hex, Sigma, ThoughtSpot, Power BI, Tableau, Metabase, Superset, Redash, QuickSight, Qlik) into a Git repo with unshackle, or when re-running a migration after the source changed. Orchestrates inventory, extract, build, translate, verify and the write-up PR.
---

# Migrate a BI tool with unshackle

unshackle's scripts do the deterministic work (listing, exporting, parsing, writing
files). You finish what they mark `TODO(unshackle)`, prove the numbers match, and open a
PR. The target layout is described in `spec/layout.md`; read it once.

Run every command from the root of the customer's target repo (or pass `--repo`).

## 0. Set up

In a target repo these skills live in `migration/skills/` (copied there by `unshackle init`
or `unshackle build`; refresh with `unshackle init --update-skills`). A reference like
`skills/translate-metrics` means `migration/skills/translate-metrics`, and
`spec/layout.md` means `migration/spec/layout.md`.

1. Pick the source skill: `skills/source-<tool>/SKILL.md` where `<tool>` is one of
   `looker lightdash omni hex sigma thoughtspot powerbi tableau metabase superset redash quicksight qlik`. Load it now; it
   covers auth, API quirks and what that tool's parser leaves as TODO.
2. Install: `uv tool install "unshackle[run]"`
   (add the tool's extra if `unshackle tools` lists one: `tableau` for `--with-data`, `looker`, `qlik`, `quicksight`). The CLI is `unshackle`.
   From a checkout: `uv pip install -e '<path>[run]'`.
3. `unshackle tools` prints the env vars each tool needs; `unshackle tools <tool>` says where
   to find each. Ask the user to export them in their shell or put them in `.env` at the
   repo root (git-ignored; unshackle reads it, and `unshackle check <tool> --prompt` writes
   it with hidden input). Then `unshackle check <tool>` confirms the server, site and token
   work. Never write a credential into a tracked file, a commit, the report or a PR description.
4. Make a branch before writing anything: `git switch -c unshackle/<tool>-migration`.
   Never commit to or push `main`.
5. `unshackle init` creates `AGENTS.md`, `unshackle.toml` and the folders without touching
   existing files. In a dbt repo it points `semantic` at `models/semantic`. If the
   repo already has its own layout, edit `[paths]` in `unshackle.toml` before building.
6. `init` doesn't assume a warehouse. Build takes the type from the export when the
   tool reports it (the report says so); otherwise it warns and labels expressions
   `ANSI_SQL`. Confirm the warehouse with the customer and set `[datasources.warehouse]
   type` in `unshackle.toml`. All SQL you write targets that dialect, and Ossie
   expressions are labelled with it.

## 1. Inventory

```
unshackle inventory <tool>        # -> migration/inventory.<tool>.md and .json
```

Show the user the inventory (sorted by views) and agree the scope before extracting.
Default proposal: everything viewed in the last 90 days, plus the models/datasets
those items depend on. Say how many items fall in and out.

Warn the user: the inventory only shows what the API token's account can see. A
non-admin account usually misses other people's personal folders, private
spaces/workspaces and usage stats. If the numbers look low, ask for an admin token.

## 2. Extract

```
unshackle extract <tool>                          # everything
unshackle extract <tool> --only <id> --only <id>  # the agreed scope (ids from the inventory)
```

Raw files land in `migration/raw/<tool>/` untouched. Check the source skill for
whether `--only` pulls dependencies automatically; if not, include the model /
dataset ids yourself.

### No API access: import downloaded files

Use `import` instead of `inventory` + `extract` when the user can't get API
credentials (no admin to create them, API off on their plan, a security policy)
but can download definitions from the tool or its Git repo:

```
unshackle import <tool> <file or folder>...    # replaces migration/raw/<tool>/
unshackle build <tool>
```

The source skill's "Manual import" section says what to download. Prefer `extract`
whenever API access is possible: downloaded files never carry usage (so scope
can't be sorted by views) or sharing permissions, usually lack the tool's generated
SQL, and for Looker and Omni carry no dashboards. The report's "imported manually"
section lists what is missing; tell the user, and agree the scope from the files
they download. On a re-import pass every file again: `import` replaces the earlier
raw files. Sigma has no file route.

## 3. Build

```
unshackle build <tool> --dry-run    # optional: see counts first
unshackle build <tool>
```

Writes `semantic/`, `catalog/`, `knowledge/`, `queries/`, `notebooks/`, `policies/`
and `migration/report.md`. Read the
report: counts, warnings (severity `block` first) and the TODO table.

## 4. Translate

```
unshackle todo       # file:line: what's left
```

Work through the list by file type, using the matching skill:

| TODO in | Skill |
| --- | --- |
| `semantic/*.ossie.yaml`, dbt: `models/semantic/*.yml` | `translate-metrics` |
| `notebooks/*.ipynb` | `translate-dashboards` |
| `policies/*` | `translate-permissions` |
| `queries/*` | `translate-metrics` (fallback section) |

Each TODO carries the original expression and its language (`[tableau] {FIXED ...}`).
Translate it, then delete the `TODO(unshackle)` marker (for YAML, delete the `todo` key
under `meta`; remove `meta` if empty). Re-run `unshackle todo` until only items you
deliberately left remain.

Never silently guess. If you are not sure a translation preserves the numbers, or the
source behaviour can't be expressed, leave the `TODO(unshackle)` in place, append your
reasoning to it, and list it under "Needs a human" in the PR description. A visible
TODO is fine; a plausible wrong number is not.

## 5. Verify

Load the `verify` skill. Write `migration/parity/*.yaml` cases for the most-viewed
dashboards and the headline metrics on them, then:

```
UNSHACKLE_WAREHOUSE_URL=<sqlalchemy url> unshackle verify
```

Fix every `differs` (the verify skill has a debugging table). A case you can't make
pass stays in the report with a note explaining why. Passing cases are copied to
`evals/`.

## 6. Write-up

1. `unshackle report` to regenerate `migration/report.md`.
2. Record conventions you discovered (fiscal calendar, currency, which tables are
   canonical, naming quirks) in `knowledge/`, and list decisions you made during
   translation and anything left for a human in the PR description.
3. Before committing, check nothing secret is staged:
   `git diff --cached | grep -inE 'password|secret|token|api[_-]?key|BEGIN .*PRIVATE'`.
   `.env`, `.unshackle/` and `*.html` previews must stay untracked. Raw exports can
   contain user names and emails; ask the user whether `migration/raw/` is committed.
4. Commit and open a PR from the branch (e.g. `gh pr create --base main`). PR body:
   the report's summary table, parity pass rate, what was out of scope, the "Needs
   a human" list, and how to re-run. Do not merge it yourself.

## Re-runs

When the source tool has changed since the last build:

```
unshackle extract <tool> [--only ...]
unshackle status            # added / changed / removed raw files per tool
unshackle build <tool>
```

(`unshackle migrate <tool> [--only ...]` does init + inventory + extract + build in one
go; use it only once the scope is settled.)

Files edited since the last build are never overwritten; they are listed under
"kept because someone edited them by hand" in the report. For each one:

```
mv notebooks/x.ipynb notebooks/x.ipynb.mine
unshackle build <tool>                 # writes the fresh version
diff -u notebooks/x.ipynb.mine notebooks/x.ipynb   # or nbdiff, from nbdime
```

Merge the upstream change into the hand-edited version, keep the human's edits,
then remove `.mine`. Catalog files are merged automatically
(human descriptions and labels win). Then re-run `unshackle todo` and `unshackle verify`,
and open a new PR.

## Checklist

- [ ] Source skill loaded; credentials only in env / `.env` / `.unshackle/`
- [ ] On a `unshackle/...` branch, not `main`
- [ ] `unshackle.toml` paths and warehouse type correct (dbt repo checked)
- [ ] Inventory reviewed; scope agreed with the user; admin-visibility caveat raised
      (manual import: the report's "imported manually" gaps shared with the user)
- [ ] Extract + build ran; report warnings read
- [ ] `unshackle todo` worked through; remaining TODOs have a reason
- [ ] Ossie documents validate (`validation/validate.py` from apache/ossie); dbt repos: `dbt parse`
- [ ] Notebooks run (`unshackle run`: no tile errors, tiles with SQL return rows) and are saved without outputs
- [ ] Parity cases written; `unshackle verify` run; diffs fixed or explained
- [ ] Conventions recorded in `knowledge/`; `unshackle report` regenerated
- [ ] No secrets staged; PR opened with summary; not merged

## Rules

- Credentials stay on the user's machine. Never echo them, log them or commit them.
- Never push to `main`; always a new branch and a PR.
- Never overwrite a file listed as a hand edit without merging it.
- Never widen access when translating permissions.
- Don't edit anything under `migration/raw/` or `migration/unshackle.lock` by hand.
- Keep original BI names as `label`s so users recognise their metrics.
