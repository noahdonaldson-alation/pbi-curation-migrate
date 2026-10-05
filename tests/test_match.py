import pytest

from pbi_migrate.extract import MissingDeletedFlag, normalize
from pbi_migrate.match import format_key, match_objects


def by_target(result):
    return {c.target.oid: c for c in result.candidates}


def test_normalize_parent_shapes():
    assert normalize("bi_report", {"id": 1, "name": "r", "deleted": False, "parent_folder": 5}).parent_oid == 5
    assert normalize("bi_report", {"id": 1, "name": "r", "deleted": False, "parent_folder": {"id": 5}}).parent_oid == 5
    assert normalize("bi_report", {"id": 1, "name": "r", "deleted": False, "parent_folder": [5]}).parent_oid == 5
    top = normalize("bi_folder", {"id": 1, "name": "ws", "deleted": False, "parent_folder": None})
    assert top.parent_oid is None and top.parent_otype is None


def test_normalize_requires_deleted_flag():
    with pytest.raises(MissingDeletedFlag):
        normalize("bi_report", {"id": 1, "name": "r"})


def test_deleted_copies_group_onto_one_target(objects):
    cand = by_target(match_objects(objects))[20]
    assert [s.oid for s in cand.sources] == [10, 11]  # trailing space on 11 is stripped
    assert format_key(cand.key) == "bi_report:Finance WS / Monthly / P&L"


def test_column_keys_through_deleted_parent(objects):
    cand = by_target(match_objects(objects))[200]
    assert [s.oid for s in cand.sources] == [100]
    assert cand.key == ("bi_report_column", "Finance WS", "Monthly", "P&L", "Revenue")


def test_unmatched_deleted(objects):
    unmatched = {o.oid for o, _ in match_objects(objects).unmatched}
    assert unmatched == {12, 101}


def test_ambiguous_is_never_paired(objects):
    result = match_objects(objects)
    assert [(o.oid, sorted(t.oid for t in ts)) for o, _, ts in result.ambiguous] == [(30, [40, 41])]
    assert 40 not in by_target(result) and 41 not in by_target(result)


def test_missing_parent_is_unkeyable(objects):
    unkeyable = match_objects(objects).unkeyable
    assert [o.oid for o, _ in unkeyable] == [13]
    assert "999" in unkeyable[0][1]


def test_active_objects_never_appear_as_sources(objects):
    result = match_objects(objects)
    for c in result.candidates:
        assert all(s.deleted for s in c.sources)
        assert not c.target.deleted


def test_match_on_external_id_resolves_name_collision(objects):
    result = match_objects(objects, "external_id")
    targets = by_target(result)
    assert [s.oid for s in targets[40].sources] == [30]  # ambiguous by name, unique by external_id
    assert [s.oid for s in targets[20].sources] == [10, 11]
    # columns have no external_id in the fixture
    assert {o.oid for o, _ in result.unkeyable} >= {100, 101}
