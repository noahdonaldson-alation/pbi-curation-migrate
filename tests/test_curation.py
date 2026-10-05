from pbi_migrate.curation import FieldWrite, is_empty, plan
from pbi_migrate.extract import BIObject
from pbi_migrate.match import Candidate

TITLE, DESC, STEWARD = 3, 4, 10001


def obj(oid, deleted=True):
    return BIObject(otype="bi_report", oid=oid, name="r", deleted=deleted)


def cand(*source_oids, target=20):
    return Candidate(("bi_report", "r"), tuple(obj(s) for s in source_oids), obj(target, deleted=False))


def ref(oid):
    return ("bi_report", oid)


def test_is_empty():
    for v in (None, "", "   ", "<p></p>", "<p>&nbsp;</p>", [], {}):
        assert is_empty(v), v
    for v in ("x", "<p>x</p>", [1], 0, False):
        assert not is_empty(v), v


def test_copies_into_empty_target():
    [p] = plan([cand(10)], {ref(10): {TITLE: "P&L", DESC: "<p>Monthly P&L</p>"}}, {ref(20): {TITLE: ""}})
    assert p.writes == [FieldWrite(TITLE, "P&L"), FieldWrite(DESC, "<p>Monthly P&L</p>")]
    assert p.skipped == []


def test_does_not_clobber_target_unless_overwrite():
    src, tgt = {ref(10): {DESC: "old"}}, {ref(20): {DESC: "new"}}
    [p] = plan([cand(10)], src, tgt)
    assert p.writes == [] and p.skipped == [(DESC, "target already has value")]
    [p] = plan([cand(10)], src, tgt, overwrite=True)
    assert p.writes == [FieldWrite(DESC, "old")]


def test_rerun_is_idempotent():
    [p] = plan([cand(10)], {ref(10): {DESC: "same"}}, {ref(20): {DESC: "same"}}, overwrite=True)
    assert p.writes == [] and p.skipped == [(DESC, "already identical")]


def test_multiple_deleted_copies_agree_or_skip():
    src = {
        ref(10): {TITLE: "P&L", DESC: "v1", STEWARD: None},
        ref(11): {TITLE: "P&L", DESC: "v2", STEWARD: [{"otype": "user", "oid": 7}]},
    }
    [p] = plan([cand(10, 11)], src, {})
    assert p.writes == [FieldWrite(TITLE, "P&L"), FieldWrite(STEWARD, [{"otype": "user", "oid": 7}])]
    assert p.skipped == [(DESC, "conflict across 2 deleted copies")]


def test_no_source_curation_yields_empty_plan():
    [p] = plan([cand(10)], {}, {})
    assert p.writes == [] and p.skipped == []
