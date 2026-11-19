# Dunder Mifflin team demo

[Deploy, update, connect and shut down](../../docs/deploy/team-demo.md).
[Claude onboarding](../../docs/deploy/claude-team-demo.md).

The generator retains every historical Parquet row and adds a deterministic 2025
sales scenario. Ten tables expose customers, products, orders, branches, employees,
shipping and reviews. Scenario and historical revenue are separate datasets.

- `fixture.py`: fresh dedicated Postgres seed, SELECT-only role, catalog and semantics.
- `business-context.md`: grains, relationships, metric definitions and caveats.
- `questions.json`: eight questions, SQL, independent arithmetic and expected results.
- `check.py`: real HTTPS MCP SDK acceptance and safety checks.
- `client_check.py`: actual Claude Code run through the existing isolated harness.
- `evidence.json`: sanitized provenance and live outcomes, without credentials/IDs.

Only generated `context/` belongs in the public context repository. Surrounding files
contain secrets and must stay private. This demo stays running until its owner chooses
shutdown; scripts never automatically delete live cloud resources.
