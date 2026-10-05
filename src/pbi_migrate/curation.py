"""Read curation from deleted objects, plan what to copy, and write it to active objects."""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pbi_migrate.client import AlationClient
from pbi_migrate.extract import BIObject
from pbi_migrate.match import Candidate

log = logging.getLogger(__name__)

CF_VALUE_PATH = "/integration/v2/custom_field_value/"
WRITE_BATCH_SIZE = 100

Ref = tuple[str, int]
FieldValues = dict[int, Any]  # field_id -> value


def title_field_id() -> int:
    return int(os.environ.get("TITLE_FIELD_ID", "3"))


def description_field_id() -> int:
    return int(os.environ.get("DESCRIPTION_FIELD_ID", "4"))


_TAG = re.compile(r"<[^>]+>|&nbsp;")


def is_empty(value: Any) -> bool:
    """None, blank strings, empty lists, and rich-text shells like '<p></p>'."""
    if value is None:
        return True
    if isinstance(value, str):
        return not _TAG.sub("", value).strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


# --- Reading ---------------------------------------------------------------------


class CurationSource(Protocol):
    def read(self, objects: Iterable[BIObject]) -> dict[Ref, FieldValues]: ...


class ApiCurationSource:
    """Custom field values (incl. title/description, if exposed) via the public API."""

    def __init__(self, client: AlationClient):
        self.client = client

    def read(self, objects: Iterable[BIObject]) -> dict[Ref, FieldValues]:
        wanted: dict[str, set[int]] = defaultdict(set)
        for o in objects:
            wanted[o.otype].add(o.oid)
        out: dict[Ref, FieldValues] = defaultdict(dict)
        for otype, oids in wanted.items():
            # VERIFY: GET filters (otype / oid) supported by custom_field_value, and
            # whether values for soft-deleted oids come back at all (open question 1).
            # Pulling per-otype then filtering client-side keeps request count low.
            for row in self.client.paginate(CF_VALUE_PATH, {"otype": otype}):
                oid = int(row["oid"])
                if oid in oids:
                    out[(otype, oid)][int(row["field_id"])] = row.get("value")
        return dict(out)


class AAv2CurationSource:
    """Fallback: read curation for soft-deleted objects from Alation Analytics (AAv2).

    Needed only if ApiCurationSource returns nothing for deleted oids. AAv2 schema
    `public` has tables like bi_datasource_columns with `deleted`, `description`,
    and custom-field joins. Not implemented until open question 1 is answered.
    """

    def read(self, objects: Iterable[BIObject]) -> dict[Ref, FieldValues]:
        raise NotImplementedError("AAv2 fallback not built yet — see CLAUDE.md open question 1")


# --- Planning --------------------------------------------------------------------


@dataclass(frozen=True)
class FieldWrite:
    field_id: int
    value: Any


@dataclass
class Plan:
    candidate: Candidate
    writes: list[FieldWrite] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (field_id, reason)

    @property
    def target(self) -> BIObject:
        return self.candidate.target


def plan(
    candidates: list[Candidate],
    source_values: dict[Ref, FieldValues],
    target_values: dict[Ref, FieldValues],
    *,
    overwrite: bool = False,
) -> list[Plan]:
    plans = []
    for cand in candidates:
        p = Plan(cand)
        # field_id -> distinct non-empty values across all deleted copies
        merged: dict[int, list[Any]] = defaultdict(list)
        for src in cand.sources:
            for fid, val in source_values.get(src.ref, {}).items():
                if not is_empty(val) and val not in merged[fid]:
                    merged[fid].append(val)

        existing = target_values.get(cand.target.ref, {})
        for fid, values in sorted(merged.items()):
            if len(values) > 1:
                p.skipped.append((fid, f"conflict across {len(cand.sources)} deleted copies"))
                continue
            value = values[0]
            current = existing.get(fid)
            if current == value:
                p.skipped.append((fid, "already identical"))
            elif not is_empty(current) and not overwrite:
                p.skipped.append((fid, "target already has value"))
            else:
                p.writes.append(FieldWrite(fid, value))
        plans.append(p)
    return plans


# --- Writing ---------------------------------------------------------------------


def apply(client: AlationClient, plans: list[Plan], batch_size: int = WRITE_BATCH_SIZE) -> int:
    """PUT every planned write in batches. Returns the number of field values sent."""
    body: list[dict[str, Any]] = []
    sent = 0

    def flush() -> None:
        nonlocal sent
        if not body:
            return
        # VERIFY: response shape — may be an async job id that should be polled.
        resp = client.put_json(CF_VALUE_PATH, list(body))
        log.info("wrote %d values: %s", len(body), resp)
        sent += len(body)
        body.clear()

    for p in plans:
        if not p.writes:
            continue
        log.info(
            "%s %s -> %s fields=%s",
            p.target.otype,
            ",".join(str(s.oid) for s in p.candidate.sources),
            p.target.oid,
            [w.field_id for w in p.writes],
        )
        for w in p.writes:
            body.append(
                {"otype": p.target.otype, "oid": p.target.oid, "field_id": w.field_id, "value": w.value}
            )
            if len(body) >= batch_size:
                flush()
    flush()
    return sent
