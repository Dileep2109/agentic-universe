"""Parse simulator event chunks and apply them once."""

from __future__ import annotations

import json
import logging
import time

from .client import AmbiguousResponse, SimClient
from .store import Store

log = logging.getLogger(__name__)


def parse_ndjson(text: str) -> list[dict]:
    """Parse NDJSON without holding a second copy of every line."""
    events = []
    line_no = 0
    start = 0
    length = len(text)
    while start < length:
        end = text.find("\n", start)
        if end < 0:
            end = length
        line_no += 1
        stripped = text[start:end].strip()
        start = end + 1
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(f"chunk line {line_no} is not JSON") from exc
    return events


def sync_chunks(client: SimClient, store: Store, kind: str) -> list[str]:
    """Ingest the next contiguous chunks of ``kind``.

    Chunk numbers are global. If the next number belongs to the other kind,
    this returns so the caller can sync that kind. Committed chunks are skipped
    inside the store, so calling this again does not double-count.
    """
    changed: list[str] = []
    while True:
        nxt = store.last_chunk + 1
        metas = client.list_chunks(kind, after=store.last_chunk)
        meta = next((item for item in metas if int(item["chunk"]) == nxt), None)
        if meta is None:
            return changed
        log.info("ingesting %s chunk %s", kind, nxt)
        changed.extend(_ingest_chunk(client, store, meta, kind))


def _ingest_chunk(client: SimClient, store: Store, meta: dict, kind: str) -> list[str]:
    """Read a chunk and apply it. A lost response is retried only before the write."""
    if not hasattr(client, "iter_chunk_events"):
        return store.apply_chunk(meta, parse_ndjson(client.get_chunk(int(meta["chunk"]))), kind)
    attempts = client.settings.idempotent_retries
    last: AmbiguousResponse | None = None
    for attempt in range(attempts):
        try:
            return store.apply_chunk(meta, client.iter_chunk_events(int(meta["chunk"])), kind)
        except AmbiguousResponse as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(client.settings.retry_sleep_s)
    assert last is not None
    raise last


def sync_all(client: SimClient, store: Store) -> list[str]:
    changed = sync_chunks(client, store, "history")
    changed.extend(sync_chunks(client, store, "events"))
    return changed
