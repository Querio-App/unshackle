# Security policy

unshackle connects to BI tools and warehouses with credentials you provide, so
we take security reports seriously.

## Reporting a vulnerability

Please **don't open a public issue.** Report it privately through
[GitHub's private vulnerability reporting](https://github.com/unshackle-analytics/unshackle/security/advisories/new).

Include what you found, how to reproduce it, and the version you used. We'll
acknowledge your report within 3 working days and keep you updated until it's
fixed. We're happy to credit you in the release notes.

## Supported versions

Only the latest release gets security fixes.

## How unshackle handles credentials

- Credentials are read from environment variables only. unshackle never writes
  them to files, the migration report, or raw exports.
- unshackle makes network calls only to the BI tool and warehouse you point it
  at. There's no telemetry.
- Raw exports in `migration/raw/` can contain business logic and user or group
  names. Review them before committing them to a shared repo.
