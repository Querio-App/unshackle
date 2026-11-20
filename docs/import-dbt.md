# Import a plain dbt project

```sh
unshackle init --repo ./context-repo
unshackle import dbt ./dbt-project --repo ./context-repo
```

The importer reads `dbt_project.yml` and its configured `model-paths` (default:
`models`). It does not connect to a warehouse, read `profiles.yml`, execute Python,
render Jinja, or invoke dbt. YAML model and column descriptions become catalog
entries; model SQL becomes reference queries, not verified results.

A local project's manifest under `target-path` (default: `target`) maps models by
`original_file_path` and project package name. `compiled_code` / `compiled_sql`
takes precedence; otherwise the importer checks the corresponding
`compiled/<project>/<original_file_path>` SQL file. Without compiled artifacts,
raw SQL is retained and unresolved Jinja receives a `TODO(unshackle)`. Refresh
artifacts with your normal dbt compile process before importing; stale artifacts
cannot be detected reliably offline. A manifest provides resolved schema and
alias; YAML `config.schema` alone may not be the final warehouse schema.
Applicable project-level `models` relation settings and inline `config(...)` calls
are preserved with pending relation/semantic TODOs until a manifest resolves them.
A compiled SQL file alone does not resolve relation configuration. Ephemeral
materializations remain pending because they do not define a warehouse table.

Legacy MetricFlow `semantic_models` entities, categorical/time dimensions, basic
measures, and simple measure-backed metrics translate into Ossie. A dbt destination
also gets MetricFlow YAML under `models/semantic`. Other destinations use
`semantic/`; all paths respect `unshackle.toml` overrides. Existing writers preserve
hand edits and other import ownership. Import into a separate context repo to avoid
duplicating your source project's semantic declarations.

Unsupported aggregations, metric types/options, templated expressions and newer
embedded semantic annotations retain their source YAML and explicit TODOs. This
includes cumulative, derived, ratio, conversion, filtered and time-spine metrics;
they are not silently approximated. Duplicate unresolved SQL model names receive
stable path-derived identifiers and a TODO instead of guessed documentation.
Descriptions for unmatched YAML models are retained as reference knowledge.
If a manifest resolves a semantic model to a different schema or alias, the model
retains its dbt ref and full source with a TODO: the shared representation cannot
express independent physical-relation and dbt-ref names. It does not silently
activate a semantic query against the wrong relation.

Only the shared YAML loader and named-model traversal are reused by Lightdash;
plain dbt import does not interpret Lightdash `meta` blocks.

The supported legacy form and newer embedded form are described in the
[dbt semantic model documentation](https://docs.getdbt.com/docs/build/semantic-models)
and [spec migration guide](https://docs.getdbt.com/docs/build/latest-metrics-spec).

Inspect the generated files and run `unshackle todo --repo ./context-repo` before
querying. Then `cd ./context-repo` to [connect locally](mcp.md) or
[refresh context](maintain.md).
