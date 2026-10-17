"""排行榜快照与「新上榜」对比测试（core/rank_history.py）。"""

import json

from core.rank_history import RankHistoryStore


def _comics(*ids):
    return [{"_id": cid, "title": f"本子{cid}", "totalLikes": 7} for cid in ids]


def test_snapshot_lives_in_persistent_dir(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.save("H24", _comics("a"))
    assert store.path("H24").parent.name == "rank_history"
    assert store.path("H24").parent.parent == tmp_path
    assert store.path("H24").name == "H24.json"


def test_first_update_has_no_baseline(tmp_path):
    store = RankHistoryStore(tmp_path)

    diff = store.update("H24", _comics("a", "b"))

    assert diff.new_entries == []
    assert diff.previous_updated_at is None
    assert [item["_id"] for item in store.load("H24")["items"]] == ["a", "b"]


def test_new_entry_is_reported_with_rank_and_baseline_time(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a", "b"))

    diff = store.update("H24", _comics("a", "new", "b"))

    assert [rank for rank, _comic in diff.new_entries] == [2]
    assert diff.new_entries[0][1]["_id"] == "new"
    assert diff.previous_updated_at is not None


def test_no_new_entry_when_board_unchanged(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a", "b"))

    assert store.update("H24", _comics("a", "b")).new_entries == []


def test_entries_beyond_display_limit_are_not_reported(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a"))

    diff = store.update("H24", _comics("a", "b", "c"), limit=2)

    assert [comic["_id"] for _rank, comic in diff.new_entries] == ["b"]
    # 但全量榜单仍然进快照，避免下次把 b 又算成新的
    assert [item["_id"] for item in store.load("H24")["items"]] == ["a", "b", "c"]


def test_history_is_isolated_per_rank_type(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a"))

    diff = store.update("D7", _comics("b"))

    assert diff.new_entries == []
    assert store.path("H24").exists() and store.path("D7").exists()


def test_returning_entry_counts_as_new_again(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a", "b"))
    store.update("H24", _comics("a"))

    diff = store.update("H24", _comics("a", "b"))

    assert [comic["_id"] for _rank, comic in diff.new_entries] == ["b"]


def test_comics_without_id_are_not_snapshotted(tmp_path):
    store = RankHistoryStore(tmp_path)

    store.update("H24", [{"title": "没有ID"}, *_comics("a")])

    assert [item["_id"] for item in store.load("H24")["items"]] == ["a"]


def test_corrupt_snapshot_is_treated_as_missing(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path("H24").write_text("{ 这不是 json", encoding="utf-8")

    diff = store.update("H24", _comics("a"))

    assert diff.new_entries == []
    assert diff.previous_updated_at is None
    assert json.loads(store.path("H24").read_text(encoding="utf-8"))["items"][0]["_id"] == "a"


def test_snapshot_with_wrong_shape_is_tolerated(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path("H24").write_text('["not", "a", "dict"]', encoding="utf-8")

    diff = store.update("H24", _comics("a"))

    assert diff.new_entries == []


def test_atomic_write_leaves_no_temp_file(tmp_path):
    store = RankHistoryStore(tmp_path)
    store.update("H24", _comics("a"))

    assert list(store.dir.glob("*.tmp")) == []


def test_load_missing_file_returns_empty(tmp_path):
    assert RankHistoryStore(tmp_path).load("D30") == {}
