"""Pull deleted + active BI objects for one BI server and normalize them."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from pbi_migrate.client import AlationClient, AlationError

log = logging.getLogger(__name__)

# otype -> path segment under /integration/v2/bi/server/{id}/
# VERIFY: endpoint paths for each otype against docs.alation.com (BI Server API v2).
ENDPOINTS: dict[str, str] = {
    "bi_folder": "folder",
    "bi_report": "report",
    "bi_datasource": "datasource",
    "bi_report_column": "report/column",
    "bi_datasource_column": "datasource/column",
    "bi_connection": "connection",
    "bi_connection_column": "connection/column",
}

# otype -> candidate (field, parent otype) pairs, first non-empty wins.
# Top-level folders (Power BI workspaces) have no parent.
# VERIFY: field names via `pbi-migrate probe` — these are the most likely guesses.
PARENT_FIELDS: dict[str, list[tuple[str, str]]] = {
    "bi_folder": [("parent_folder", "bi_folder")],
    "bi_report": [("parent_folder", "bi_folder")],
    "bi_datasource": [("parent_folder", "bi_folder")],
    "bi_report_column": [("report", "bi_report")],
    "bi_datasource_column": [("datasource", "bi_datasource")],
    "bi_connection": [("datasource", "bi_datasource")],
    "bi_connection_column": [("connection", "bi_connection")],
}

# VERIFY: which flag BI v2 objects carry for soft-delete.
DELETED_FIELDS = ("deleted", "is_deleted")


class BIObject(BaseModel):
    model_config = ConfigDict(frozen=True)

    otype: str
    oid: int
    name: str
    deleted: bool
    external_id: str | None = None
    parent_otype: str | None = None
    parent_oid: int | None = None

    @property
    def ref(self) -> tuple[str, int]:
        return (self.otype, self.oid)


class MissingDeletedFlag(ValueError):
    pass


def _ref_id(value: Any) -> int | None:
    """Parent references may come back as an id, {"id": ...}, or a list of either."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        ids = [i for i in (_ref_id(v) for v in value) if i is not None]
        if len(ids) > 1:
            log.warning("multiple parents %s; using none", ids)
            return None
        return ids[0] if ids else None
    if isinstance(value, dict):
        return _ref_id(value.get("id"))
    return int(value)


def normalize(otype: str, raw: dict[str, Any]) -> BIObject:
    deleted = next((raw[f] for f in DELETED_FIELDS if f in raw), None)
    if deleted is None:
        raise MissingDeletedFlag(
            f"{otype} {raw.get('id')} has none of {DELETED_FIELDS}; "
            "run `pbi-migrate probe` and update DELETED_FIELDS in extract.py"
        )
    parent_otype = parent_oid = None
    for field, p_otype in PARENT_FIELDS.get(otype, []):
        parent_oid = _ref_id(raw.get(field))
        if parent_oid is not None:
            parent_otype = p_otype
            break
    ext = raw.get("external_id")
    return BIObject(
        otype=otype,
        oid=int(raw["id"]),
        name=str(raw.get("name") or ""),
        deleted=bool(deleted),
        external_id=str(ext) if ext not in (None, "") else None,
        parent_otype=parent_otype,
        parent_oid=parent_oid,
    )


def fetch_raw(client: AlationClient, server_id: int, otype: str) -> list[dict[str, Any]]:
    """Fetch every object of one otype, deleted and active, deduped by id.

    Server-side deleted filtering is inconsistent across endpoints (seen in the field),
    so we ask both with and without the filter and partition on the object's own flag.
    """
    path = f"/integration/v2/bi/server/{server_id}/{ENDPOINTS[otype]}/"
    by_id: dict[int, dict[str, Any]] = {}
    # VERIFY: whether `deleted=true` is accepted / needed to surface soft-deleted objects.
    for params in ({}, {"deleted": "true"}):
        try:
            for raw in client.paginate(path, params):
                by_id[int(raw["id"])] = raw
        except AlationError as e:
            if not params:
                raise
            log.info("%s rejected %s (%s); relying on unfiltered list", path, params, e)
    return list(by_id.values())


def extract_server(
    client: AlationClient, server_id: int, otypes: list[str] | None = None
) -> list[BIObject]:
    objects: list[BIObject] = []
    for otype in otypes or list(ENDPOINTS):
        raws = fetch_raw(client, server_id, otype)
        objects.extend(normalize(otype, r) for r in raws)
        log.info("%s: %d objects", otype, len(raws))
    return objects
