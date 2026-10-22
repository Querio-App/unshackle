---
name: translate-permissions
description: Use when translating BI security (content permissions, row-level security, column/object-level security, user attributes, access grants) into open policies - Cedar in policies/*.cedar and row rules in policies/row_filters.*.yml - or when resolving TODO(unshackle) items in policies/.
---

# Translate BI permissions into Cedar and row filters

Two layers, both in `policies/`:

- **Cedar** (`schema.cedarschema`, `<tool>.cedar`): may this user `view`, `query` or
  `edit` this `Datasource`, `Table`, `Column`, `Metric` or `Notebook`?
- **Row filters** (`row_filters.<tool>.yml`): SQL predicates added to every query on
  a table, with `{{ user.<attr> }}` filled from the querying user's IdP attributes.

The build emits what it could parse; each item carries `// source:` (Cedar) or
`source:` (YAML) with the original rule. Unparsed rules have `TODO(unshackle)`.

## Principles

1. **Keep existing behaviour.** Don't assume the source was deny-by-default. Cedar
   denies anything not permitted, so reproduce the source's real defaults
   explicitly (if every user could query the connection, write that permit).
   Otherwise the migration silently removes access.
2. **Never widen access.** If a rule can only be expressed more loosely, don't. Use
   the narrower form, keep the TODO, and flag it.
3. **Groups come from the IdP, not the repo.** Use the IdP group names the source
   tool synced (Okta/Azure AD/Google groups). Don't invent groups or list members in
   the repo. Map tool-local groups/roles to IdP groups in a table in the PR; if a
   group has no IdP equivalent, flag it.
4. **Review before enforcing.** Generated policies are a proposal. The PR must say
   they need an admin's review, and list every rule you changed, added or couldn't map.
5. **Flag what can't be expressed** (per-user one-off shares, schedule/alert
   recipients, download/export rights, embedding secrets). Leave `// TODO(unshackle): ...`
   with the source rule and list it under "Needs a human".

## Entity ids

| Entity | Id |
| --- | --- |
| `Datasource` | datasource name in `unshackle.toml` (e.g. `warehouse`) |
| `Table` | `<datasource>.<schema>.<table>` |
| `Column` | `<datasource>.<schema>.<table>.<column>` |
| `Metric` | metric name in `semantic/` (snake_case) |
| `Notebook` | notebook file stem (`revenue-overview`) |
| `Group` | IdP group name |
| `User` | email |

Row filters use `table: <schema>.<table>` as it appears in SQL.

## Cedar examples

Group can view a dashboard:

```
// source: tableau://project/Finance capability Read
permit (
  principal in Group::"finance",
  action == Action::"view",
  resource in Notebook::"revenue-overview"
);
```

Group can query everything in a datasource (tables, columns, metrics sit under it):

```
permit (
  principal in Group::"analysts",
  action in [Action::"query", Action::"view"],
  resource in Datasource::"warehouse"
);
```

Nobody queries a column unless they are in a group:

```
forbid (
  principal,
  action == Action::"query",
  resource in Column::"warehouse.analytics.customers.email"
)
unless { principal in Group::"pii-readers" };
```

Editors of a notebook:

```
permit (principal in Group::"finance-editors", action == Action::"edit", resource in Notebook::"revenue-overview");
```

`forbid` always beats `permit`. The schema's `User` only has `email`; if you need
another attribute in a `when` clause, add it to `schema.cedarschema` and say so in
the PR. Validate if the Cedar CLI is available:
`cedar validate --schema policies/schema.cedarschema --policies policies/<tool>.cedar`.

## Row filter examples

```yaml
row_filters:
- name: region_scope
  table: analytics.fct_orders
  predicate: region = {{ user.region }}
  groups: [sales]
  source: looker access_filter orders.region -> user_attribute region
- name: own_accounts
  table: analytics.dim_accounts
  predicate: owner_email = {{ user.email }}
  groups: ['*']
  source: tableau user filter [Owner Email] = USERNAME()
- name: entitled_regions
  table: analytics.fct_orders
  predicate: region in (select region from security.user_regions where email = {{ user.email }})
  groups: ['*']
  source: powerbi RLS role "Regional" via Security[Email] = USERPRINCIPALNAME()
```

`groups` is who the rule applies to (`['*']` = everyone). A user outside those
groups is not filtered, so make sure unrestricted groups really were unrestricted in
the source. Predicates are SQL in the warehouse dialect on the table's own columns
(or a subquery). Multiple rules on a table are ANDed; if the source ORs roles
(Power BI: a user in two roles sees the union), write one predicate with `or`.

## Mapping per tool

| Tool | Source construct | Target |
| --- | --- | --- |
| Tableau | project / workbook / view permissions (View, Interact, Edit) | Cedar `view` / `edit` on `Notebook`; project defaults apply to each workbook in it |
| Tableau | datasource permissions (Connect) | `query` on `Datasource` / `Table` |
| Tableau | user filter calc `[Region] = USERNAME()` | row filter `region = {{ user.email }}` (check whether USERNAME is the email or a login; say which attribute) |
| Tableau | `ISMEMBEROF('Finance')` | `groups: [finance]`, or `or` branches per group |
| Tableau | entitlement table joined to `USERNAME()` | subquery predicate on the entitlement table |
| Looker | `access_filter: {field, user_attribute}` | row filter on the explore's base table: `<col> = {{ user.<attr> }}` |
| Looker | `access_grant` + `required_access_grants` on field/view/explore | `forbid query ... unless { principal in Group::... }` on Column/Table; map `allowed_values` to groups |
| Looker | roles = permission set + model set | `query` on the model's tables for the role's groups; `edit` where the permission set has `develop`/`save_content` |
| Looker | folder access (view/manage) | `view` / `edit` on the notebooks from that folder |
| Power BI | RLS role DAX `[Region] = "EU"` | row filter per role with `groups` = role members |
| Power BI | `USERPRINCIPALNAME()` / `USERNAME()` | `{{ user.email }}` |
| Power BI | OLS (object-level security) | `forbid query` on Column/Table unless group |
| Power BI | workspace roles / app audiences | Viewer -> `view`; Contributor/Member/Admin -> `view` + `edit` on its notebooks |
| Power BI | dataset Build permission | `query` on `Datasource` |
| Sigma | team permissions on workspace/workbook (view/explore/edit) | `view` / `edit` on `Notebook` |
| Sigma | column-level security | `forbid query` on Column unless group |
| Sigma | `CurrentUserAttributeText("x")`, `CurrentUserInTeam("T")` in filters | row filter with `{{ user.x }}` / `groups` |
| ThoughtSpot | sharing (can view / can edit) on liveboards, answers, worksheets | `view` / `edit` on Notebook; worksheet share -> `query` on its tables |
| ThoughtSpot | RLS rule `region = ts_groups` / `ts_username` | row filter (`ts_groups` -> a predicate per group or group-to-value table) |
| ThoughtSpot | column-level security | `forbid query` on Column unless group |
| Omni | `access_filters` with user attributes on topics | row filter |
| Omni | `access_grants` / `required_access_grants` | `forbid query` on Column/Table unless group |
| Omni | folder/document roles (viewer, editor) | `view` / `edit` on Notebook |
| Lightdash | `sql_filter` with `${lightdash.attributes.x}` on a model | row filter with `{{ user.x }}` |
| Lightdash | `required_attributes` on model/dimension/metric | `forbid query` on Table/Column/Metric unless group (attribute value -> group) |
| Lightdash | space access (viewer/editor) | `view` / `edit` on Notebook |
| Hex | project sharing (can view / explore / edit), workspace groups | `view` / `edit` on Notebook |
| Hex | data connection access | `query` on `Datasource` |

Attribute-to-group mapping: Cedar only knows groups, so an attribute test like
`department = "finance"` becomes `principal in Group::"finance"` only if that IdP
group has exactly those users. If not, keep it as a row filter or flag it.

## Done when

- Every source permission and RLS rule appears in `policies/` or in the PR's "Needs
  a human" list.
- Baseline access matches the source (write the defaults explicitly).
- No rule grants more than the source did.
- The PR has the tool-group -> IdP-group mapping table and says "review before
  enforcing".
