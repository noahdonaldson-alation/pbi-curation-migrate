# CLAUDE.md — Power BI curation migration

## What this project is

A Python CLI that copies curated metadata (title, description, custom field values, tags where possible) from **soft-deleted** Power BI objects in an Alation catalog to the **re-extracted replacement objects** that represent the same thing.

**Why:** the Power BI Scanner OCF connector's parser soft-deletes any asset it cannot positively confirm still exists on re-ingestion. The re-extracted object comes back with a new internal ID and no curation, so stewards lose their work on every MDE. This is parser behaviour rather than something a catalog setting can switch off, so the curation has to be carried across after the fact.

**Deliverable:** a script + README that a catalog admin can run themselves.

## Prior art

There are internal one-off scripts that solve the same problem for other object families — RDBMS schema/table/column curation migration between data sources, and a server-side script for SSRS. Neither is runnable here (one is RDBMS-shaped, the other runs in the tenant's Django shell) but their matching logic is the reference for how deleted objects are keyed to new ones. Check for a current version before extending the matcher.

Alation Analytics (AAv2) still holds the curation for soft-deleted objects. If the public API won't return custom field values for deleted objects, AAv2 is the fallback source.

## Layout

```
pbi-curation-migrate/
  CLAUDE.md
  README.md              # customer-facing: prereqs, token, dry-run, run
  pyproject.toml
  .env.example           # copy to .env (git-ignored)
  src/pbi_migrate/
    client.py            # thin Alation REST client (token auth, retry, paging)
    extract.py           # pull deleted + active BI objects for a BI server
    match.py             # pair deleted -> active by fully-qualified name (or external_id)
    curation.py          # read curation from source, plan + write to target
    cli.py               # `probe` and `run` subcommands
  tests/
    fixtures/            # small synthetic JSON samples of API responses
    test_match.py
    test_curation.py
  reports/               # CSV output lands here (git-ignored except .gitkeep)
```

## Flow

1. **Extract** — for a given BI server ID, list all objects by otype (`bi_folder`, `bi_report`, `bi_datasource`, `bi_datasource_column`, `bi_report_column`, `bi_connection`, `bi_connection_column`). Pull both deleted and active sets. Server-side deleted filtering is inconsistent across endpoints — nested `/folder/` arrays have been seen not to filter deleted while `/report/?oids=` does — so filtering is done client-side on the object's own flag, never trusting the server filter alone.
2. **Match** — build a fully-qualified key per object: `workspace / folder path / object name` for reports and datasets; append `column name` for fields. Keys walk parents through deleted *and* active objects, so a deleted column under a deleted report still keys correctly. Pair each key's deleted object(s) to exactly one active object. Emit unmatched-deleted and ambiguous (1→many) lists; never guess on ambiguous.
   - Repeated MDEs can leave several deleted copies of the same object. These are grouped as multiple sources for one target; `curation.plan` copies a field only if every non-empty source value agrees, otherwise skips it as a conflict.
3. **Read curation** from the deleted object(s): title, description, and every custom field value. Public API first (`ApiCurationSource`); AAv2 fallback (`AAv2CurationSource`, stub) if the API refuses deleted objects.
4. **Write** to the active object. Skip any field where the target already has a non-empty value unless `--overwrite`. Batch writes (100/request); log every object written with source oid → target oid.
5. **Report** — always produce a CSV: `otype, source_oid, target_oid, key, fields_copied, skipped_reason`. Dry-run is the default; `--apply` is required to write.

## APIs (verify against docs.alation.com before relying on field names)

Every assumption about the API shape is tagged `VERIFY:` in the code — `grep -rn VERIFY src/` lists them. `pbi-migrate probe` exists to settle them against a real tenant.

- BI object read: `GET /integration/v2/bi/server/{server_id}/{otype}/` with `?oids=` and `?limit/skip` paging.
- Custom field values: `GET/PUT /integration/v2/custom_field_value/` (bulk; body is a list of `{otype, oid, field_id, value}`).
- Titles/descriptions on BI objects: check whether `custom_field_value` with the built-in title/description field IDs works for BI otypes, or whether the BI v2 endpoints accept `PUT` on `title`/`description` directly. The CF Value API covers most curation, but RDBMS titles/descriptions are only reachable through the RDBMS APIs — BI may be the same.
- Auth: `TOKEN` header with an API access token generated from a refresh token (`POST /integration/v1/createAPIAccessToken/`). Base URL, user ID and refresh token come from env vars / `.env`, never from code.

## Non-goals (v1)

- Lineage, tags, policies, article links, domain membership, home-page references.
- Anything that deletes or un-deletes objects.
- Running in the tenant's Django shell.

## Working conventions

- Python 3.11+, `httpx` for HTTP, `pydantic` for response models, `rich` for the console report. Standard library otherwise.
- Default to dry-run. `--apply` is required to write. Print the write count and ask for confirmation unless `--yes`.
- Every write is idempotent: re-running after success copies nothing (targets already populated).
- **No customer data in the repo** — this repo is public. No customer or account names, no support case numbers, no colleague names, no internal URLs or repo paths, in code, comments, commit messages or fixtures. Fixtures are synthetic. `.env` and `reports/*.csv` are git-ignored.
- Tests run with `pytest`; matching logic must be unit-tested against fixtures before anything touches a live tenant.
- Test first against a demo tenant, then a customer sandbox if one exists, then prod with dry-run and the customer reviewing the CSV before `--apply`.

## Open questions to settle early

1. Does `/integration/v2/bi/server/{id}/report/` return soft-deleted objects at all, and with their curation? If not, AAv2 is mandatory for step 3. → `pbi-migrate probe`
2. Which field IDs are title/description for BI otypes on the target instance (`GET /integration/v2/custom_field/`). → `pbi-migrate probe --fields`
3. Does the Power BI Scanner change the object `external_id` on re-extraction, or only the internal oid? If `external_id` is stable, match on that instead of the name path — far safer. → `run --match-on external_id`
4. How many deleted objects are in play on the target instance (size the batch/perf question before worrying about it).
5. Has the underlying Power BI soft-delete issue been resolved? If not, this runs on a schedule after every MDE, not once.

## Context links

- Power BI Scanner docs: https://documentation.alation.com/en/latest/OpenConnectorFramework/BIConnectors/PowerBIScannerOCFConnector/

Engagement-specific notes (account, contacts, case numbers, internal prior art) live in `CLAUDE.local.md`, which is git-ignored and stays off this public repo.
