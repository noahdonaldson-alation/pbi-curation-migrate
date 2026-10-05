# Power BI curation migration

When the Power BI Scanner connector re-extracts your tenant, it can soft-delete a report, dataset or field and create a new copy with a new ID. The new copy has none of the titles, descriptions or custom field values your stewards entered. This tool finds each deleted object and its active replacement, then copies the curation across.

It **never deletes or restores objects**. It only writes custom field values (including title and description) to active objects. By default it writes nothing and only produces a report.

## What you need

- Python 3.11 or newer
- An Alation account that can edit the Power BI objects (Catalog Admin or Server Admin is simplest)
- The ID of your Power BI BI server in Alation. Open the server in the catalog; the number in the URL `/bi/v2/server/<ID>/` is the ID.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Then fill in `.env`:

| Variable | Where to find it |
|---|---|
| `ALATION_BASE_URL` | Your Alation URL, e.g. `https://yourco.alationcloud.com` |
| `ALATION_USER_ID` | Your numeric user ID (shown in the URL of your profile page) |
| `ALATION_REFRESH_TOKEN` | Profile → **Authentication** → **Create Refresh Token**. Treat it like a password. |

`.env` is ignored by git. Don't commit it or share it.

## 1. Probe (first run only)

```bash
pbi-migrate probe --server-id 12 --fields
```

This step is read-only. It shows how many deleted and active objects the API returns for each object type, prints one raw sample per type, and lists your custom fields with their IDs. Check that:

- the **deleted** column is not zero for types where you know objects were deleted, and
- `TITLE_FIELD_ID` / `DESCRIPTION_FIELD_ID` in `.env` match the title and description rows in the custom field list.

## 2. Dry run (the default)

```bash
pbi-migrate run --server-id 12
```

This writes a CSV to `reports/` and prints a summary. **Nothing is written to Alation.** Each CSV row is one deleted object:

| Column | Meaning |
|---|---|
| `otype` | Object type (`bi_report`, `bi_datasource_column`, …) |
| `source_oid` | Deleted object ID(s). There can be several if the object was deleted on more than one extraction. |
| `target_oid` | Active object that receives the curation (blank if none) |
| `key` | Workspace / folder / name path used to match the two |
| `fields_copied` | Custom field IDs that would be (or were) copied |
| `skipped_reason` | Why a field or object was skipped |

Common skip reasons:

- **no active match**: there's no current object with the same path. It may have been really removed from Power BI.
- **ambiguous**: more than one active object has the same path. The tool won't guess; curate these by hand.
- **target already has value**: someone has already curated the new object. Its value is kept.
- **conflict across N deleted copies**: older deleted copies have different values for this field, so it is skipped.

Review the CSV before going on.

## 3. Apply

```bash
pbi-migrate run --server-id 12 --apply
```

The tool shows how many values it will write and asks you to type `yes`. Add `--yes` to skip the prompt, for example in a scheduled job.

You can safely run it again. Objects that already have the curation are skipped, so a second run writes nothing new.

## Options

| Flag | Effect |
|---|---|
| `--otypes bi_report bi_datasource` | Limit the run to some object types |
| `--match-on external_id` | Match on the Power BI ID instead of the name path. Use this if `probe` shows the external ID stays the same across re-extraction. It is safer. |
| `--overwrite` | Replace values already on the target. Use with care. |
| `--report path.csv` | Choose where the CSV is written |
| `-v` | Verbose logging |

## Development

```bash
pytest
```

Tests use synthetic fixtures in `tests/fixtures/` and never call a live tenant. The `VERIFY:` comments in `src/` mark API details that still need to be confirmed against a real instance (`grep -rn VERIFY src/`).
