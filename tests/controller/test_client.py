"""Client reconciliation: duplicate ids and lost responses are not assumed successes."""

import httpx
import pytest

from controller.client import AmbiguousResponse, SimClient, SimError
from controller.settings import RunSettings


def _client(handler) -> SimClient:
    settings = RunSettings(idempotent_retries=3, retry_sleep_s=0)
    return SimClient("http://sim.test", settings=settings, transport=httpx.MockTransport(handler))


def test_duplicate_proposal_is_success_only_when_the_server_has_it():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/approvals":
            return httpx.Response(409, json={"error": "duplicate_proposal_id", "detail": "prop-1"})
        if request.method == "GET" and request.url.path == "/approvals":
            return httpx.Response(200, json=[{
                "approval_id": "apr_00009",
                "proposal_id": "prop-1",
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": 3,
                "changed_dims": ["voice"],
            }])
        return httpx.Response(404, json={"error": "not_found", "detail": "no"})

    result = _client(handler).request_approval({
        "proposal_id": "prop-1",
        "workflow_id": "nw_renewal",
        "base_version": 3,
        "proposed_config": {},
    })
    assert result.ok is True
    assert result.reconciled is True
    assert result.body["approval_id"] == "apr_00009"


def test_duplicate_proposal_without_a_server_record_is_not_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"error": "duplicate_proposal_id", "detail": "missing"})
        return httpx.Response(200, json=[])

    with pytest.raises(SimError) as caught:
        _client(handler).request_approval({"proposal_id": "missing", "workflow_id": "nw_renewal"})
    assert caught.value.code == "duplicate_proposal_unconfirmed"
    assert caught.value.ambiguous is False


def test_lost_approval_response_uses_server_state():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"error": "server_error", "detail": "boom"})
        if request.method == "GET" and request.url.path == "/approvals":
            return httpx.Response(200, json=[{
                "approval_id": "apr_00002",
                "proposal_id": "prop-2",
                "workflow_id": "ks_cancel",
                "status": "pending",
                "base_version": 1,
                "changed_dims": ["schedule"],
            }])
        return httpx.Response(404, json={"detail": "no"})

    result = _client(handler).request_approval({"proposal_id": "prop-2", "workflow_id": "ks_cancel"})
    assert result.ok and result.reconciled
    assert result.body["approval_id"] == "apr_00002"


def test_lost_approval_response_with_no_record_stays_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json=[])

    with pytest.raises(AmbiguousResponse):
        _client(handler).request_approval({"proposal_id": "prop-3", "workflow_id": "nw_kyc"})


def test_auto_apply_duplicate_matches_config_history_not_a_guess():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "duplicate_proposal_id", "detail": "auto-1"})
        if request.url.path == "/approvals":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/config/history"):
            return httpx.Response(200, json=[{
                "version": 4,
                "actor": "controller_auto",
                "changed_dims": ["caller_id"],
                "proposal_id": "auto-1",
                "approval_id": None,
            }])
        return httpx.Response(404, json={"detail": "no"})

    result = _client(handler).auto_apply("nw_renewal", {"proposal_id": "auto-1", "base_version": 3})
    assert result.ok and result.reconciled
    assert result.body["version"] == 4


def test_auto_apply_id_used_by_an_approval_is_not_reported_as_applied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "duplicate_proposal_id", "detail": "shared"})
        if request.url.path == "/approvals":
            return httpx.Response(200, json=[{
                "approval_id": "apr_00003",
                "proposal_id": "shared",
                "workflow_id": "nw_renewal",
                "status": "pending",
                "base_version": 1,
                "changed_dims": ["voice"],
            }])
        return httpx.Response(200, json=[])

    with pytest.raises(SimError) as caught:
        _client(handler).auto_apply("nw_renewal", {"proposal_id": "shared"})
    assert caught.value.code == "proposal_id_used_by_approval"


def test_apply_conflict_reconciles_only_when_status_is_applied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"error": "not_approved", "detail": {"status": "applied"}})
        if request.url.path == "/approvals/apr_00004":
            return httpx.Response(200, json={
                "approval_id": "apr_00004",
                "proposal_id": "prop-4",
                "workflow_id": "nw_renewal",
                "status": "applied",
                "base_version": 2,
                "changed_dims": ["voice"],
            })
        if request.url.path.endswith("/config/history"):
            return httpx.Response(200, json=[{
                "version": 5,
                "actor": "controller_approved",
                "changed_dims": ["voice"],
                "proposal_id": "prop-4",
                "approval_id": "apr_00004",
            }])
        return httpx.Response(404, json={"detail": "no"})

    result = _client(handler).apply_approval("nw_renewal", "apr_00004")
    assert result.ok and result.reconciled
    assert result.body["config"]["version"] == 5


def test_advance_server_error_is_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(AmbiguousResponse) as caught:
        _client(handler).advance(15)
    assert caught.value.ambiguous is True


def test_idempotent_get_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"scenario": "fake", "sim_minute": 0, "last_event_id": 0})

    state = _client(handler).state()
    assert state["scenario"] == "fake"
    assert calls["n"] == 3
