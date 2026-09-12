"""HTTP API end to end: IAM token → scopes → instance lifecycle → event feed."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import IamIssuer

pytestmark = [pytest.mark.db]


def test_requires_bearer_and_scopes(client: TestClient, iam: IamIssuer) -> None:
    tenant = uuid.uuid4()
    assert client.get("/api/v1/definitions").status_code == 401
    wrong_audience = iam.auth(tenant, audience="control-plane")
    assert client.get("/api/v1/definitions", headers=wrong_audience).status_code == 401
    reader = iam.auth(tenant, scopes=["process:read"])
    assert client.get("/api/v1/definitions", headers=reader).status_code == 200
    denied = client.post(
        "/api/v1/instances", json={"workflow_id": "three_step_approval"}, headers=reader
    )
    assert denied.status_code == 403


def test_full_happy_path_and_events(client: TestClient, iam: IamIssuer) -> None:
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    h = iam.auth(tenant, principal_id=principal)

    defs = client.get("/api/v1/definitions", headers=h).json()
    assert any(d["workflow_id"] == "three_step_approval" for d in defs)

    started = client.post(
        "/api/v1/instances",
        json={"workflow_id": "three_step_approval", "initial_variables": {"requester": "alice"}},
        headers=h,
    )
    assert started.status_code == 201, started.text
    instance = started.json()
    assert instance["status"] == "waiting" and instance["current_tasks"] == ["initial_review"]
    iid = instance["id"]

    inbox = client.get("/api/v1/inbox?role=reviewers", headers=h).json()
    assert any(t["task_id"] == "initial_review" for t in inbox)

    step1 = client.post(
        f"/api/v1/instances/{iid}/tasks/initial_review/complete",
        json={"form_payload": {"initial_decision": "approve"}},
        headers=h,
    )
    assert step1.status_code == 200, step1.text
    assert step1.json()["current_tasks"] == ["final_approval"]

    step2 = client.post(
        f"/api/v1/instances/{iid}/tasks/final_approval/complete",
        json={"form_payload": {"final_decision": "approve"}},
        headers=h,
    )
    assert step2.status_code == 200 and step2.json()["status"] == "completed"
    assert step2.json()["outcome"] == "approve"

    diagram = client.get(f"/api/v1/instances/{iid}/diagram", headers=h).json()
    assert "three_step_approval" in diagram["bpmn_xml"]

    page = client.get("/api/v1/events?limit=100", headers=h).json()
    types = [e["type"] for e in page["items"]]
    assert types[0] == "platform.workflow.started.v1"
    assert "platform.workflow.completed.v1" in types
    assert page["nextCursor"] is None
    # Cursor paging and tenant isolation.
    first = client.get("/api/v1/events?limit=1", headers=h).json()
    assert first["nextCursor"] == first["items"][0]["cursor"]
    other = client.get("/api/v1/events", headers=iam.auth(uuid.uuid4())).json()
    assert other["items"] == []


def test_tenant_isolation_and_not_found(client: TestClient, iam: IamIssuer) -> None:
    owner = iam.auth(uuid.uuid4())
    iid = client.post(
        "/api/v1/instances", json={"workflow_id": "three_step_approval"}, headers=owner
    ).json()["id"]
    stranger = iam.auth(uuid.uuid4())
    assert client.get(f"/api/v1/instances/{iid}", headers=stranger).status_code == 404
    assert client.get(f"/api/v1/instances/{uuid.uuid4()}", headers=owner).status_code == 404


def test_cancel_and_migrate_need_right_scopes(client: TestClient, iam: IamIssuer) -> None:
    tenant = uuid.uuid4()
    writer = iam.auth(tenant)
    iid = client.post(
        "/api/v1/instances", json={"workflow_id": "three_step_approval"}, headers=writer
    ).json()["id"]
    forbidden = client.post(
        f"/api/v1/instances/{iid}/migrate",
        json={"from_version": 1, "to_version": 1, "task_id_mapping": {}},
        headers=writer,
    )
    assert forbidden.status_code == 403
    cancelled = client.post(
        f"/api/v1/instances/{iid}/cancel", json={"reason": "no longer needed"}, headers=writer
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    again = client.post(f"/api/v1/instances/{iid}/cancel", json={"reason": "twice"}, headers=writer)
    assert again.status_code == 409


def test_healthz_reports_definitions(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["definitions"] >= 3
