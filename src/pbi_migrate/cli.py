"""pbi-migrate: copy curation from soft-deleted Power BI objects to their replacements.

    pbi-migrate probe --server-id 12            # inspect API shapes; settle open questions
    pbi-migrate run   --server-id 12            # dry-run: CSV report only (default)
    pbi-migrate run   --server-id 12 --apply    # write, after confirmation
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from pbi_migrate import curation
from pbi_migrate.client import AlationClient, AlationError, Settings, load_dotenv
from pbi_migrate.extract import DELETED_FIELDS, ENDPOINTS, extract_server, fetch_raw
from pbi_migrate.match import MatchResult, format_key, match_objects

console = Console()
REPORT_COLUMNS = ["otype", "source_oid", "target_oid", "key", "fields_copied", "skipped_reason"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pbi-migrate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="Inspect raw API responses for a BI server")
    probe.add_argument("--server-id", type=int, required=True)
    probe.add_argument("--otypes", nargs="+", choices=list(ENDPOINTS), default=list(ENDPOINTS))
    probe.add_argument("--samples", type=int, default=1, help="Raw records to print per otype")
    probe.add_argument("--fields", action="store_true", help="Also list custom field definitions")

    run = sub.add_parser("run", help="Match deleted -> active and copy curation")
    run.add_argument("--server-id", type=int, required=True)
    run.add_argument("--otypes", nargs="+", choices=list(ENDPOINTS), default=list(ENDPOINTS))
    run.add_argument("--match-on", choices=["name", "external_id"], default="name")
    run.add_argument("--report", type=Path, help="CSV path (default: reports/<server>_<ts>.csv)")
    run.add_argument("--overwrite", action="store_true",
                     help="Overwrite fields that already have a value on the target")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only (the default)")
    mode.add_argument("--apply", action="store_true", help="Write curation to targets")
    run.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    settings = Settings.from_env()
    try:
        with AlationClient(settings) as client:
            if args.command == "probe":
                return cmd_probe(client, args)
            return cmd_run(client, settings, args)
    except AlationError as e:
        console.print(f"[red]Alation API error:[/red] {e}")
        return 1


# --- probe -----------------------------------------------------------------------


def cmd_probe(client: AlationClient, args: argparse.Namespace) -> int:
    table = Table(title=f"BI server {args.server_id}")
    for col in ("otype", "total", "deleted", "active", "no flag", "has external_id"):
        table.add_column(col)
    for otype in args.otypes:
        try:
            raws = fetch_raw(client, args.server_id, otype)
        except AlationError as e:
            table.add_row(otype, f"[red]{e.status_code}[/red]", "", "", "", "")
            continue
        flags = Counter(next((str(r[f]) for f in DELETED_FIELDS if f in r), "missing") for r in raws)
        table.add_row(
            otype, str(len(raws)), str(flags.get("True", 0)), str(flags.get("False", 0)),
            str(flags.get("missing", 0)), str(sum(1 for r in raws if r.get("external_id"))),
        )
        for raw in raws[: args.samples]:
            console.rule(f"{otype} sample")
            console.print_json(json.dumps(raw, default=str))
    console.print(table)

    if args.fields:
        fields = client.get_json("/integration/v2/custom_field/")
        ft = Table(title="Custom fields")
        for col in ("id", "name_singular", "field_type", "builtin_name"):
            ft.add_column(col)
        for f in fields:
            ft.add_row(*(str(f.get(c, "")) for c in ("id", "name_singular", "field_type", "builtin_name")))
        console.print(ft)
    return 0


# --- run -------------------------------------------------------------------------


def cmd_run(client: AlationClient, settings: Settings, args: argparse.Namespace) -> int:
    objects = extract_server(client, args.server_id, args.otypes)
    result = match_objects(objects, args.match_on)

    source = curation.ApiCurationSource(client)
    sources = [s for c in result.candidates for s in c.sources]
    targets = [c.target for c in result.candidates]
    plans = curation.plan(
        result.candidates, source.read(sources), source.read(targets), overwrite=args.overwrite
    )

    report_path = args.report or Path("reports") / (
        f"server{args.server_id}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    write_report(report_path, result, plans)
    print_summary(objects, result, plans)
    console.print(f"Report: [bold]{report_path}[/bold]")

    n_values = sum(len(p.writes) for p in plans)
    n_objects = sum(1 for p in plans if p.writes)
    if not args.apply:
        console.print(f"[yellow]Dry run[/yellow] — would write {n_values} values to {n_objects} "
                      "objects. Re-run with --apply to write.")
        return 0
    if n_values == 0:
        console.print("Nothing to write.")
        return 0
    if not args.yes:
        answer = console.input(
            f"Write {n_values} field values to {n_objects} objects on "
            f"[bold]{settings.base_url}[/bold]? Type 'yes' to continue: "
        )
        if answer.strip().lower() != "yes":
            console.print("Aborted; nothing written.")
            return 1
    sent = curation.apply(client, plans)
    console.print(f"[green]Wrote {sent} field values.[/green]")
    return 0


def write_report(path: Path, result: MatchResult, plans: list[curation.Plan]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REPORT_COLUMNS)
        for p in plans:
            c = p.candidate
            reasons = "; ".join(f"{fid}: {why}" for fid, why in p.skipped)
            if not p.writes and not p.skipped:
                reasons = "no curation on source"
            w.writerow([
                c.target.otype,
                ";".join(str(s.oid) for s in c.sources),
                c.target.oid,
                format_key(c.key),
                ";".join(str(x.field_id) for x in p.writes),
                reasons,
            ])
        for obj, key in result.unmatched:
            w.writerow([obj.otype, obj.oid, "", format_key(key), "", "no active match"])
        for obj, key, targets in result.ambiguous:
            oids = ",".join(str(t.oid) for t in targets)
            w.writerow([obj.otype, obj.oid, "", format_key(key), "",
                        f"ambiguous: {len(targets)} active matches ({oids})"])
        for obj, why in result.unkeyable:
            w.writerow([obj.otype, obj.oid, "", obj.name, "", f"unkeyable: {why}"])


def print_summary(objects, result: MatchResult, plans: list[curation.Plan]) -> None:
    rows: dict[str, Counter] = {}

    def bump(otype: str, col: str, n: int = 1) -> None:
        rows.setdefault(otype, Counter())[col] += n

    for o in objects:
        bump(o.otype, "deleted" if o.deleted else "active")
    for p in plans:
        bump(p.target.otype, "matched")
        bump(p.target.otype, "values to write", len(p.writes))
    for o, _ in result.unmatched:
        bump(o.otype, "unmatched")
    for o, _, _ in result.ambiguous:
        bump(o.otype, "ambiguous")
    for o, _ in result.unkeyable:
        bump(o.otype, "unkeyable")

    cols = ["deleted", "active", "matched", "unmatched", "ambiguous", "unkeyable", "values to write"]
    table = Table(title="Summary")
    table.add_column("otype")
    for col in cols:
        table.add_column(col, justify="right")
    for otype, counts in rows.items():
        table.add_row(otype, *(str(counts.get(c, 0)) for c in cols))
    console.print(table)


if __name__ == "__main__":
    raise SystemExit(main())
