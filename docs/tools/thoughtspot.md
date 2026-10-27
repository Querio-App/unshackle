# ThoughtSpot

unshackle takes your ThoughtSpot models, liveboards, saved answers, row and column security and sharing out of ThoughtSpot and turns them into Ossie metrics, notebooks and Cedar policies in your repo.

## Install

```bash
pip install unshackle      # in a virtual environment; or: uv tool install unshackle
```

## Connect

| Variable | Where to find it |
| --- | --- |
| `THOUGHTSPOT_URL` | The address you open ThoughtSpot at, no path: `https://<cluster>.thoughtspot.cloud` |
| `THOUGHTSPOT_USERNAME` | Login name of a service user. Admin > Users lists it |
| `THOUGHTSPOT_PASSWORD` or `THOUGHTSPOT_SECRET_KEY` | The user's password, or the trusted-authentication secret key (Develop > Security settings). The secret key wins if both are set |
| `THOUGHTSPOT_ORG_ID` | Optional. Numeric org id on multi-org clusters. Run once per org |

- Use an admin, or a user who can manage data and sees all content. The user only exports what it can see; the rest shows up as errors in the report.
- Check that your ThoughtSpot contract allows exporting your own TML and SQL through the REST API.

`unshackle check thoughtspot --prompt` asks for these and saves them to `.env` (git-ignored); `unshackle check thoughtspot` tests the connection.

## Run

```bash
unshackle migrate thoughtspot
```

Lists models, liveboards and answers, exports their TML, generated SQL and sharing, and writes the repo. Add `--only <guid>` to migrate one object.

## What you get

| In ThoughtSpot | In your repo |
| --- | --- |
| Table | Catalog entry with column types and descriptions |
| Model / worksheet | One Ossie semantic model per physical table, joins as entities |
| Attribute and measure columns, simple formulas | Dimensions, measures and metrics (simple, filtered, ratio, derived) |
| Descriptions, synonyms, AI context | `knowledge/thoughtspot-model-<name>.md` |
| Spotter coaching (business terms, reference questions) | `knowledge/thoughtspot-spotter-feedback.md` |
| Liveboard | Notebook: one tile per visualization, running ThoughtSpot's own SQL; tabs and notes kept |
| Liveboard filters | Dashboard filters |
| Saved answer | Verified query in `queries/` |
| Row-level security on a table | Row filter |
| Column security | Cedar `forbid` on the column |
| Sharing (view / edit) | Cedar `permit` on the notebook or the model's tables |

## What needs finishing

- **Advanced formulas**: `group_aggregate` and other level-of-detail functions, `cumulative_*`, `moving_*`, `rank`, `last_value`. Rewrite as SQL models or window functions.
- **Liveboard filters**: baked into each tile's SQL as fixed values. Swap them for the named bind and set a default.
- **Column order in tile SQL**: when unshackle can't confirm which output is which answer column, check it by hand.
- **Chart types with no match** (geo, treemap, sankey, waterfall, funnel, pivot): pick the closest chart.
- **RLS with `ts_groups` or `ts_username`**: map to your identity provider's groups or emails.
- **SQL views**: recreate them as warehouse views.

`unshackle todo` lists them. The guide explains how to finish each.

## Without API access

Without a service user, export TML from the UI: on a liveboard, answer or model, open the more (`...`) menu > Export TML, with associated objects to get the models and tables too.

```bash
unshackle import thoughtspot "Sales Overview.zip" more-tml/
unshackle build thoughtspot
```

Import the `.zip`, `.tml` files or a folder of them. Files can't carry:

- ThoughtSpot's generated SQL: liveboard tiles get a TODO to write it, and saved answers don't become verified queries.
- View counts and last-viewed dates.
- Sharing (who can view or edit). Row-level security in table TML is kept.
- Links back to ThoughtSpot.

## Check the numbers

ThoughtSpot's generated SQL for each liveboard tile or answer is the reference. Put it in a case with the date window pinned, and compare it with the migrated tile. You can also pull numbers straight from ThoughtSpot through its search data API or a liveboard CSV export. Pin relative filters on both sides.

Write each comparison as a case in `migration/parity/` and run `unshackle verify`. See the [verify guide](../../skills/verify/SKILL.md).

## More

The [source-thoughtspot guide](../../skills/source-thoughtspot/SKILL.md) has the full details, written for coding agents and anyone finishing the migration.
