"""HTTP client for the simulator contract in docs/SIMULATOR.md.

GET requests are retried: they do not change the world. POST and PUT are not
retried blindly. When the outcome of a write is unclear, or the server reports
a duplicate proposal id, the client reads the server's current approvals and
config history and only then reports success or failure.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx

from .settings import RunSettings


class SimError(Exception):
    def __init__(self, status: int, code: str, detail: Any, *, ambiguous: bool = False):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.ambiguous = ambiguous


class AmbiguousResponse(SimError):
    """The call may or may not have changed the simulator. Reconcile before retrying."""

    def __init__(self, status: int, code: str, detail: Any):
        super().__init__(status, code, detail, ambiguous=True)


@dataclass(frozen=True)
class ReconciledWrite:
    """A write whose outcome was checked against simulator state."""

    ok: bool
    reconciled: bool
    body: dict
    reason: str


class SimClient:
    def __init__(
        self,
        base_url: str,
        settings: RunSettings | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings or RunSettings()
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_s, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SimClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- reads (safe to retry) ---

    def health(self) -> dict:
        return self._json("GET", "/health")

    def state(self) -> dict:
        return self._json("GET", "/sim/state")

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict]:
        if kind not in ("events", "history"):
            raise ValueError("kind must be 'events' or 'history'")
        data = self._json("GET", "/events/chunks", params={"kind": kind, "after": after})
        return list(data)

    def get_chunk(self, n: int) -> str:
        response = self._send(
            "GET",
            f"/events/chunks/{n}",
            idempotent=True,
            timeout=self.settings.chunk_timeout_s,
        )
        return response.text

    def iter_chunk_events(self, n: int) -> Iterator[dict]:
        """Yield one chunk's events without retaining the response body.

        One attempt. The caller retries a failed stream before anything is written.
        """
        try:
            with self._http.stream(
                "GET",
                f"/events/chunks/{n}",
                timeout=self.settings.chunk_timeout_s,
            ) as response:
                if response.status_code >= 500:
                    response.read()
                    raise AmbiguousResponse(response.status_code, "server_error", response.text)
                if response.status_code >= 400:
                    response.read()
                    raise self._error_from_response(response)
                for line_no, line in enumerate(response.iter_lines(), start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        yield json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"chunk line {line_no} is not JSON") from exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise AmbiguousResponse(0, "transport", str(exc)) from exc

    def workflows(self) -> list[dict]:
        return list(self._json("GET", "/workflows"))

    def workflow(self, workflow_id: str) -> dict:
        return self._json("GET", f"/workflows/{workflow_id}")

    def get_config(self, workflow_id: str) -> dict:
        return self._json("GET", f"/workflows/{workflow_id}/config")

    def config_history(self, workflow_id: str) -> list[dict]:
        return list(self._json("GET", f"/workflows/{workflow_id}/config/history"))

    def list_approvals(
        self, workflow_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        params = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return list(self._json("GET", "/approvals", params=params or None))

    def get_approval(self, approval_id: str) -> dict:
        return self._json("GET", f"/approvals/{approval_id}")

    def suggest_fixes(self, workflow_id: str, problem: str) -> dict:
        """Ask for up to three prompt suggestions. Costs credits and is not a ranking.

        The call is not retried: a lost response may already have spent credits.
        """
        if not workflow_id or not problem:
            raise ValueError("workflow_id and problem are required")
        return self._json(
            "POST",
            "/llm/suggest-fixes",
            json={"workflow_id": workflow_id, "problem": problem},
            idempotent=False,
        )

    def start_validation(self, workflow_id: str, prompt_version: str, n: int) -> dict:
        """Safety pass-rate for one prompt. This does not measure engagement.

        ``n`` must be in the documented range [50, 2000]. The call is not retried.
        """
        if not 50 <= int(n) <= 2000:
            raise ValueError("validation n must be between 50 and 2000")
        return self._json(
            "POST",
            "/validation/runs",
            json={"workflow_id": workflow_id, "prompt_version": prompt_version, "n": int(n)},
            idempotent=False,
        )

    # --- time ---

    def advance(self, minutes: int) -> dict:
        """Advance simulated time. A lost response is ambiguous: do not call this again
        until ``state()`` shows whether ``sim_minute`` moved.
        """
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        return self._json("POST", "/sim/advance", json={"minutes": minutes}, idempotent=False)

    # --- writes, reconciled ---

    def request_approval(self, body: dict) -> ReconciledWrite:
        proposal_id = body.get("proposal_id")
        workflow_id = body.get("workflow_id")

        def locate() -> dict | None:
            return self.find_approval_by_proposal(str(proposal_id), workflow_id)

        try:
            created = self._json("POST", "/approvals", json=body, idempotent=False)
        except SimError as exc:
            return self._reconcile_existing(
                exc,
                locate,
                duplicate_code="duplicate_proposal_id",
                found_reason="approval already exists for this proposal_id",
                missing_duplicate_code="duplicate_proposal_unconfirmed",
            )
        return ReconciledWrite(True, False, created, "created")

    def auto_apply(self, workflow_id: str, body: dict) -> ReconciledWrite:
        proposal_id = body.get("proposal_id")

        def locate() -> dict | None:
            if not proposal_id:
                return None
            owned = self.find_approval_by_proposal(str(proposal_id), workflow_id)
            if owned is not None:
                # This id was consumed by the approval path. That is not an auto-apply.
                raise SimError(
                    409,
                    "proposal_id_used_by_approval",
                    {"proposal_id": proposal_id, "approval": owned},
                )
            return self.find_config_by_proposal(workflow_id, str(proposal_id))

        try:
            applied = self._json(
                "PUT", f"/workflows/{workflow_id}/config", json=body, idempotent=False
            )
        except SimError as exc:
            return self._reconcile_existing(
                exc,
                locate,
                duplicate_code="duplicate_proposal_id",
                found_reason="config history already contains this proposal_id",
                missing_duplicate_code="duplicate_proposal_unconfirmed",
            )
        return ReconciledWrite(True, False, applied, "applied")

    def apply_approval(self, workflow_id: str, approval_id: str) -> ReconciledWrite:
        def locate() -> dict | None:
            approval = self.get_approval(approval_id)
            status = approval.get("status")
            if status == "applied":
                version = self.find_config_by_approval(workflow_id, approval_id)
                return {"approval": approval, "config": version}
            if status == "approved":
                return None
            raise SimError(409, "apply_not_landed", approval)

        try:
            applied = self._json(
                "POST",
                f"/workflows/{workflow_id}/apply",
                json={"approval_id": approval_id},
                idempotent=False,
            )
        except SimError as exc:
            if exc.code in ("approval_lapsed", "not_approved", "not_found", "stale_base_version"):
                # Confirmed terminal outcome. ``not_approved`` can also mean the
                # approval was already applied, so look before accepting the error.
                if exc.code == "not_approved":
                    found = self._locate_quietly(locate)
                    if found is not None:
                        return ReconciledWrite(True, True, found, "approval already applied")
                if exc.ambiguous:
                    raise
                raise
            return self._reconcile_existing(
                exc,
                locate,
                duplicate_code="",
                found_reason="approval is already applied",
                missing_duplicate_code="apply_unconfirmed",
            )
        return ReconciledWrite(True, False, applied, "applied")

    def find_approval_by_proposal(
        self, proposal_id: str, workflow_id: str | None = None
    ) -> dict | None:
        rows = self.list_approvals(workflow_id)
        matches = [row for row in rows if row.get("proposal_id") == proposal_id]
        if len(matches) > 1:
            raise SimError(409, "ambiguous_proposal_match", matches)
        return matches[0] if matches else None

    def find_config_by_proposal(self, workflow_id: str, proposal_id: str) -> dict | None:
        matches = [
            row
            for row in self.config_history(workflow_id)
            if row.get("proposal_id") == proposal_id
        ]
        if len(matches) > 1:
            raise SimError(409, "ambiguous_config_match", matches)
        return matches[0] if matches else None

    def find_config_by_approval(self, workflow_id: str, approval_id: str) -> dict | None:
        matches = [
            row
            for row in self.config_history(workflow_id)
            if row.get("approval_id") == approval_id
        ]
        if len(matches) > 1:
            raise SimError(409, "ambiguous_config_match", matches)
        return matches[0] if matches else None

    def _reconcile_existing(
        self,
        exc: SimError,
        locate: Callable[[], dict | None],
        *,
        duplicate_code: str,
        found_reason: str,
        missing_duplicate_code: str,
    ) -> ReconciledWrite:
        is_duplicate = bool(duplicate_code) and exc.code == duplicate_code
        if not exc.ambiguous and not is_duplicate:
            raise exc
        found = self._locate_quietly(locate)
        if found is not None:
            return ReconciledWrite(True, True, found, found_reason)
        if is_duplicate:
            raise SimError(
                exc.status,
                missing_duplicate_code,
                {"error": exc.code, "detail": exc.detail},
            ) from exc
        raise exc

    @staticmethod
    def _locate_quietly(locate: Callable[[], dict | None]) -> dict | None:
        try:
            return locate()
        except SimError as exc:
            if exc.code in ("proposal_id_used_by_approval", "apply_not_landed", "ambiguous_proposal_match"):
                raise
            if exc.ambiguous:
                raise AmbiguousResponse(exc.status, "reconcile_unconfirmed", exc.detail) from exc
            raise

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._send(method, path, **kwargs)
        if not response.content:
            return None
        return response.json()

    def _send(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool = True,
        json: Any = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        attempts = self.settings.idempotent_retries if idempotent else 1
        last: AmbiguousResponse | None = None
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    method, path, json=json, params=params, timeout=timeout
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = AmbiguousResponse(0, "transport", str(exc))
            else:
                if response.status_code >= 500:
                    last = AmbiguousResponse(response.status_code, "server_error", response.text)
                elif response.status_code >= 400:
                    raise self._error_from_response(response)
                else:
                    return response
            if attempt + 1 < attempts:
                time.sleep(self.settings.retry_sleep_s)
        assert last is not None
        raise last

    @staticmethod
    def _error_from_response(response: httpx.Response) -> SimError:
        detail: Any
        code = "http_error"
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            code = str(body.get("error") or code)
            detail = body.get("detail", body)
        else:
            detail = response.text
        return SimError(response.status_code, code, detail)
