"""Pair soft-deleted BI objects with their active replacements.

Pure functions over BIObject lists — no I/O — so this is fully unit-testable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pbi_migrate.extract import BIObject

Key = tuple[str, ...]
MatchOn = Literal["name", "external_id"]


class UnkeyableError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    """One active target and every deleted copy that maps to it.

    Several deleted copies is normal when the object was soft-deleted on more than
    one MDE run; curation.plan resolves them field by field.
    """

    key: Key
    sources: tuple[BIObject, ...]
    target: BIObject


@dataclass
class MatchResult:
    candidates: list[Candidate] = field(default_factory=list)
    unmatched: list[tuple[BIObject, Key]] = field(default_factory=list)
    ambiguous: list[tuple[BIObject, Key, list[BIObject]]] = field(default_factory=list)
    unkeyable: list[tuple[BIObject, str]] = field(default_factory=list)


def format_key(key: Key) -> str:
    otype, *parts = key
    return f"{otype}:{' / '.join(parts)}"


def name_key(obj: BIObject, index: dict[tuple[str, int], BIObject]) -> Key:
    """otype + name path from the workspace down, e.g. (bi_report, WS, Folder, Report).

    Walks parents through the full index (deleted and active), so a deleted column
    under a deleted report produces the same key as its active counterpart.
    """
    parts = [obj.name.strip()]
    seen = {obj.ref}
    cur = obj
    while cur.parent_oid is not None:
        ref = (cur.parent_otype or "", cur.parent_oid)
        if ref in seen:
            raise UnkeyableError(f"parent cycle at {ref}")
        parent = index.get(ref)
        if parent is None:
            raise UnkeyableError(f"parent {ref[0]} {ref[1]} not in extract")
        parts.append(parent.name.strip())
        seen.add(ref)
        cur = parent
    return (obj.otype, *reversed(parts))


def external_id_key(obj: BIObject) -> Key:
    if not obj.external_id:
        raise UnkeyableError("no external_id")
    return (obj.otype, obj.external_id)


def match_objects(objects: list[BIObject], match_on: MatchOn = "name") -> MatchResult:
    index = {o.ref: o for o in objects}
    result = MatchResult()
    deleted: dict[Key, list[BIObject]] = defaultdict(list)
    active: dict[Key, list[BIObject]] = defaultdict(list)

    for obj in objects:
        try:
            key = name_key(obj, index) if match_on == "name" else external_id_key(obj)
        except UnkeyableError as e:
            if obj.deleted:
                result.unkeyable.append((obj, str(e)))
            continue
        (deleted if obj.deleted else active)[key].append(obj)

    for key, sources in sorted(deleted.items()):
        targets = active.get(key, [])
        if not targets:
            result.unmatched.extend((s, key) for s in sources)
        elif len(targets) > 1:
            result.ambiguous.extend((s, key, targets) for s in sources)
        else:
            sources.sort(key=lambda o: o.oid)
            result.candidates.append(Candidate(key, tuple(sources), targets[0]))
    return result
