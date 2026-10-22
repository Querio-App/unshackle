# Contributing to unshackle

Thanks for helping. unshackle gets definitions out of BI tools and into open
formats in a Git repo. Most of the work is in two places: **sources** (reading a
BI tool) and **targets** (writing open formats).

## Set up

```bash
git clone https://github.com/unshackle-data/unshackle
cd unshackle
uv venv && uv pip install -e ".[dev]"
pytest
./examples/tableau-demo/run.sh
```

Both must pass before you open a pull request.

## How the code fits together

| Path | What it does |
| --- | --- |
| `src/unshackle/ir.py` | The neutral model every source produces and every target reads |
| `src/unshackle/sources/<tool>.py` | `inventory`, `extract` (network) and `parse` (offline); see `sources/base.py` |
| `src/unshackle/target/` | Writers that turn the model into files |
| `tests/fixtures/<tool>/` | Saved exports that `parse` is tested against |
| `skills/` | Instructions for people and coding agents finishing a migration |

Rules that keep output trustworthy:

- `parse()` never touches the network. It's tested from `tests/fixtures/<tool>/`.
- If something can't be translated faithfully, keep the original in
  `source_expression` and set `todo`. A visible TODO beats a wrong number.
- Generated SQL divides with `1.0 * a / nullif(b, 0)`.

## Common contributions

**Fix a translation.** Add the smallest fixture that shows the problem under
`tests/fixtures/<tool>/`, write a failing test, then fix the parser.

**Share a fixture.** Real exports are the most useful thing you can give us.
Remove names, emails, hostnames and anything confidential first, and open a
"Fixture submission" issue.

**Add a source.** Open a "New source" issue first so we can agree on scope.
Copy the shape of an existing source (`lightdash.py` is a good start): an
`inventory`, an `extract` that saves raw files, and an offline `parse`.

**Add a target.** Targets read the model in `ir.py` and write files. If you
build a target for your own product, we're happy to take it.

## Pull requests

- Keep each pull request to one change, with a test.
- Sign off every commit (see below).
- Describe what changed and why. Mention which tool versions you tested against.

## Sign your commits (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a contributor license agreement. It says you have the right to submit
your change under the project's license. Add a sign-off line to every commit:

```bash
git commit -s -m "Fix Tableau LOD parsing"
```

That adds `Signed-off-by: Your Name <you@example.com>`. CI checks for it. To fix
commits you already made: `git rebase --signoff main`, then force-push your
branch.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).

## Conduct

Everyone taking part follows our [Code of Conduct](CODE_OF_CONDUCT.md).
