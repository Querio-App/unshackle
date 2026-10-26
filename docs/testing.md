# How sources are tested

Each source is tested offline on saved exports in `tests/fixtures/`: exports over a
fictional dataset (`tests/data/dunder_mifflin/`), and for Looker and Power BI,
open-source projects people built. The generated SQL runs in DuckDB on that dataset,
and expected numbers come from the tool's documented semantics. Translation work
doesn't need a BI account.

If you use one of these tools, an anonymized export (a "Fixture submission" issue) is
the most useful thing you can contribute. See [CONTRIBUTING.md](../CONTRIBUTING.md).
