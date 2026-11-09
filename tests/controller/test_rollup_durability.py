"""Durability of grouped totals, and ingest memory while a chunk is folded."""

import sqlite3
import tracemalloc

import httpx
import pytest

from controller.analytics import feature_levels, period_counts
from controller.client import AmbiguousResponse, SimClient
from controller.ingest import _ingest_chunk
from controller.settings import RunSettings
from controller.store import Store


def _lead(event_id, workflow, campaign, person, lang="hi"):
    return {
        "type": "lead",
        "ts": "2026-12-20T08:00:00+05:30",
        "workflow_id": workflow,
        "campaign_id": campaign,
        "lead_id": f"{workflow}:{person}",
        "person_id": f"{workflow}:p{person}",
        "features": {"lang": lang, "city_tier": "1", "age_band": "18-30", "value_band": "mid"},
        "event_id": event_id,
    }


def _call(event_id, workflow, campaign, person, connected=True):
    return {
        "type": "call",
        "ts": "2026-12-20T09:00:00+05:30",
        "workflow_id": workflow,
        "campaign_id": campaign,
        "lead_id": f"{workflow}:{person}",
        "person_id": f"{workflow}:p{person}",
        "attempt_no": 1,
        "campaign_day_index": 0,
        "config_version": 1,
        "cohort_id": "default",
        "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
        "connected": connected,
        "engaged": False,
        "disposition": "short_hangup" if connected else "no_answer",
        "qa": None,
        "event_id": event_id,
    }


def _meta(chunk):
    return {"chunk": chunk, "until_ts": "2026-12-20T09:00:00+05:30"}


def test_a_failed_rollup_write_rolls_back_the_events_and_replays_once(tmp_path, monkeypatch):
    store = Store.open(tmp_path, "atomic")
    def fail(self, deltas):
        raise RuntimeError("killed during aggregate update")
    monkeypatch.setattr(Store, "_flush_call_rollups", fail)
    with pytest.raises(RuntimeError, match="aggregate update"):
        store.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")], "history")
    assert store.chunk_applied(0) is False
    assert period_counts(store, "north", "all")["dials"] == 0
    assert store.lead_count() == 0
    monkeypatch.undo()
    store.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")], "history")
    assert period_counts(store, "north", "all")["dials"] == 1
    assert period_counts(store, "north", "all")["leads_uploaded"] == 1
    store.close()

    reopened = Store.open(tmp_path, "atomic")
    assert reopened.chunk_applied(0) is True
    assert period_counts(reopened, "north", "all")["dials"] == 1
    reopened.close()


def test_replay_and_overlapping_ids_do_not_double_count(tmp_path):
    store = Store.open(tmp_path, "replay")
    first = [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")]
    store.apply_chunk(_meta(0), first, "history")
    store.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a"), _call(3, "north", "north:c", "a")], "history")
    assert period_counts(store, "north", "all")["dials"] == 1
    overlap = [_call(2, "north", "north:c", "a"), _call(3, "north", "north:c", "a")]
    store.apply_chunk(_meta(1), overlap, "history")
    assert period_counts(store, "north", "all")["dials"] == 2
    store.close()


def test_duplicate_lead_is_not_a_correction(tmp_path):
    store = Store.open(tmp_path, "correct")
    original = _lead(1, "north", "north:c", "a", lang="hi")
    corrected = _lead(2, "north", "north:c", "a", lang="en:xx\x1f")
    store.apply_chunk(_meta(0), [original, corrected, _call(3, "north", "north:c", "a")], "history")
    row = store.conn.execute("SELECT lang FROM leads WHERE lead_id = 'north:a'").fetchone()
    assert row["lang"] == "hi"
    assert period_counts(store, "north", "all")["leads_uploaded"] == 1
    levels, _total = feature_levels(store, "north", "history")
    langs = {level.value: level.leads for level in levels if level.feature == "lang"}
    assert langs == {"hi": 1}
    store.close()


def test_interrupted_rebuild_retries_without_losing_committed_events(tmp_path, monkeypatch):
    store = Store.open(tmp_path, "rebuild")
    store.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")], "history")
    store.conn.execute("DELETE FROM controller_meta")

    def killed(self):
        self.conn.execute("DELETE FROM call_group_totals")
        raise RuntimeError("rebuild interrupted")

    monkeypatch.setattr(Store, "_rebuild_group_totals", killed)
    with pytest.raises(RuntimeError, match="interrupted"):
        store._ensure_group_totals()
    monkeypatch.undo()
    assert period_counts(store, "north", "all")["dials"] == 1
    store._ensure_group_totals()
    assert period_counts(store, "north", "all")["dials"] == 1
    assert store.conn.execute("SELECT value FROM controller_meta WHERE key = 'call_rollups'").fetchone()[0] == "1"
    store.close()


def test_database_without_rollup_tables_rebuilds_on_open(tmp_path):
    path = tmp_path / "legacy.sqlite"
    first = Store.open(path.parent, "legacy")
    first.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")], "history")
    first.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE call_group_totals")
    raw.execute("DROP TABLE lead_group_totals")
    raw.execute("DROP TABLE controller_meta")
    raw.commit()
    raw.close()

    reopened = Store(path, "legacy")
    assert period_counts(reopened, "north", "history")["dials"] == 1
    assert period_counts(reopened, "north", "history")["leads_uploaded"] == 1
    assert reopened.conn.execute("SELECT COUNT(*) FROM call_group_totals").fetchone()[0] > 0
    reopened.close()
    again = Store(path, "legacy")
    assert period_counts(again, "north", "all")["dials"] == 1
    again.close()


def test_counts_above_32_bits_round_trip(tmp_path):
    store = Store.open(tmp_path, "wide")
    store.conn.execute(
        """INSERT INTO call_agg (
               workflow_id, kind, campaign_id, config_version, cohort_id, call_date, hour,
               campaign_day_index, lang, city_tier, age_band, value_band, welcome_message,
               voice, caller_id, prompt_version, dials, connected, engaged, halluc_flags,
               concluded_interested, concluded_not_interested, no_answer, voicemail, short_hangup
           ) VALUES (
               'north', 'history', 'c', 1, 'default', '2026-12-20', 9, 0,
               'hi', '1', '18-30', 'mid', 'W1', 'V1', 'D1', 'P1',
               ?, 1, 0, 0, 0, 0, 0, 0, 0
           )""",
        (3_000_000_000,),
    )
    store.conn.execute("DELETE FROM controller_meta")
    store._ensure_group_totals()
    assert period_counts(store, "north", "history")["dials"] == 3_000_000_000
    store.close()


def test_a_reader_does_not_see_an_uncommitted_chunk(tmp_path):
    store = Store.open(tmp_path, "read")
    store.apply_chunk(_meta(0), [_lead(1, "north", "north:c", "a"), _call(2, "north", "north:c", "a")], "history")
    reader = sqlite3.connect(store.path)
    before = reader.execute(
        "SELECT dials FROM call_group_totals WHERE workflow_id = 'north' AND grain = '_all'"
    ).fetchone()[0]
    with store.transaction():
        store.conn.execute(
            """UPDATE call_group_totals SET dials = dials + 9
               WHERE workflow_id = 'north' AND grain = '_all'"""
        )
        during = reader.execute(
            "SELECT dials FROM call_group_totals WHERE workflow_id = 'north' AND grain = '_all'"
        ).fetchone()[0]
        assert during == before
    after = reader.execute(
        "SELECT dials FROM call_group_totals WHERE workflow_id = 'north' AND grain = '_all'"
    ).fetchone()[0]
    assert after == before + 9
    reader.close()
    store.close()


def test_feature_values_that_contain_separators_stay_in_one_workflow(tmp_path):
    store = Store.open(tmp_path, "sep")
    tricky = "en:1\x1f2"
    store.apply_chunk(
        _meta(0),
        [_lead(1, "north", "north:c", "a", lang=tricky), _call(2, "north", "north:c", "a"),
         _lead(3, "south", "south:c", "b", lang="hi"), _call(4, "south", "south:c", "b")],
        "history",
    )
    north, _total = feature_levels(store, "north", "history")
    south, _total = feature_levels(store, "south", "history")
    assert {level.value for level in north if level.feature == "lang"} == {tricky}
    assert {level.value for level in south if level.feature == "lang"} == {"hi"}
    assert period_counts(store, "north", "all")["dials"] == 1
    assert period_counts(store, "south", "all")["dials"] == 1
    store.close()


def test_missing_lead_is_counted_and_not_stored_as_null(tmp_path):
    store = Store.open(tmp_path, "missing")
    call = _call(1, "north", "north:c", "ghost")
    store.apply_chunk(_meta(0), [call], "history")
    assert period_counts(store, "north", "all")["dials"] == 1
    assert period_counts(store, "north", "all")["leads_uploaded"] == 0
    row = store.conn.execute(
        "SELECT group_key FROM call_group_totals WHERE grain = 'features'"
    ).fetchone()
    assert row["group_key"] is not None
    store.close()


def test_a_generator_chunk_matches_a_materialized_chunk_and_holds_less(tmp_path):
    listed = Store.open(tmp_path / "listed", "mem")
    streamed = Store.open(tmp_path / "streamed", "mem")
    try:
        def one(i):
            return _call(i + 1, "north", "north:c", "a")

        tracemalloc.start()
        listed.apply_chunk(_meta(0), [one(i) for i in range(4000)], "history")
        _current, peak_list = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        tracemalloc.start()
        streamed.apply_chunk(_meta(0), (one(i) for i in range(4000)), "history")
        _current, peak_stream = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert period_counts(listed, "north", "all")["dials"] == 4000
        assert period_counts(streamed, "north", "all")["dials"] == 4000
        assert peak_stream < peak_list
    finally:
        listed.close()
        streamed.close()


def test_a_lost_chunk_stream_is_retried_before_the_write(tmp_path):
    store = Store.open(tmp_path, "retry")
    event = _call(1, "north", "north:c", "a")

    class Flaky:
        settings = RunSettings(idempotent_retries=2, retry_sleep_s=0)

        def __init__(self):
            self.calls = 0

        def iter_chunk_events(self, n):
            self.calls += 1
            if self.calls == 1:
                def fail():
                    yield event
                    raise AmbiguousResponse(0, "transport", "reset")
                return fail()
            return iter([event])

    client = Flaky()
    assert _ingest_chunk(client, store, _meta(0), "history") == []
    assert client.calls == 2
    assert period_counts(store, "north", "all")["dials"] == 1
    store.close()


def test_streamed_chunk_matches_parsed_ndjson():
    body = "\n".join([
        "",
        '{"event_id": 1, "type": "campaign_created"}',
        '{"event_id": 2, "type": "lead"}',
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/events/chunks/4"
        return httpx.Response(200, text=body)

    client = SimClient("http://simulator.example", transport=httpx.MockTransport(handler))
    try:
        assert list(client.iter_chunk_events(4)) == [
            {"event_id": 1, "type": "campaign_created"},
            {"event_id": 2, "type": "lead"},
        ]
    finally:
        client.close()
