"""Real mobile authentication and PostgreSQL; only APNs transport is replaced."""

import asyncio
import copy
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import VerifiedMobilePrincipal
from app.config import settings
from app.database import sync_session
from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.models import (
    ContentPlan,
    CreationThread,
    Job,
    MobileSession,
    NativeActivity,
    NotificationDelivery,
    NotificationInstallation,
    PlanItem,
    User,
)
from app.routes import auth, native_activity, notification_installations
from app.services.apns import Accepted, APNsTransport, Rejected, TransportUncertain
from app.services.device_render import device_record, pin_device_request
from app.services.mobile_auth import issue_access_token, refresh_token_hash
from app.services.native_activity import activity_statement
from app.services.notification_delivery import (
    NotificationPolicy,
    RegistrationConflict,
    discover_deliveries,
    dispatch_delivery,
    notification_policy,
    register_installation,
)
from app.services.token_crypto import _fernet, decrypt_token
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

TOKEN = "ab" * 32
POLICY = NotificationPolicy(enabled=True, topic="com.example.kria")


class RecordingSender:
    def __init__(self, result=None, during_send=None):
        self.result = result if result is not None else Accepted()
        self.during_send = during_send
        self.calls = []

    async def send(self, target, notification):
        self.calls.append((target, notification))
        if self.during_send:
            await self.during_send()
        return self.result


@dataclass
class Journey:
    client: TestClient
    app: FastAPI
    owner: uuid.UUID
    thread_id: uuid.UUID
    installation_id: uuid.UUID
    session_id: uuid.UUID
    family_id: uuid.UUID
    refresh_token: str = field(repr=False)
    headers: dict = field(repr=False)
    job_id: uuid.UUID

    def register(self, *, token=TOKEN, enabled=True, installation_id=None, headers=None):
        return self.client.put(
            f"/me/notification-installations/{installation_id or self.installation_id}",
            json={"token": token, "enabled": enabled},
            headers=headers or self.headers,
        )

    def activity(self):
        from tests.routes.test_native_activity_postgres import settle

        with sync_session() as db:
            job = db.get(Job, self.job_id)
            settle(job, "published")
            db.execute(activity_statement(job, "first"))
            db.commit()
            return db.scalar(select(NativeActivity.id).where(NativeActivity.job_id == self.job_id))

    def another_project(self):
        with sync_session() as db:
            original = db.get(Job, self.job_id)
            item = db.get(PlanItem, original.content_plan_item_id)
            sibling = PlanItem(
                content_plan_id=item.content_plan_id, position=99, idea="Other video"
            )
            db.add(sibling)
            db.flush()
            job = Job(
                user_id=self.owner,
                status="awaiting_device",
                raw_storage_path="test-only",
                content_plan_item_id=sibling.id,
                content_plan_ownership_epoch=0,
                assembly_plan={
                    "variants": [{"variant_id": "first", "render_generation_id": "approved"}]
                },
            )
            db.add(job)
            db.flush()
            sibling.current_job_id = job.id
            thread = CreationThread(
                creator_id=self.owner,
                content_plan_id=item.content_plan_id,
                active_plan_item_id=sibling.id,
                active_job_id=job.id,
            )
            db.add(thread)
            recipe = EditRecipeV1.model_validate(
                device_record(original, "first")["status"]["request"]["recipe"]
            )
            request = make_device_request(
                job_id=job.id, variant_id="first", revision=1, recipe=recipe
            )
            pin_device_request(job, request, base_generation="approved")
            db.commit()
            return Journey(
                self.client,
                self.app,
                self.owner,
                thread.id,
                self.installation_id,
                self.session_id,
                self.family_id,
                self.refresh_token,
                self.headers,
                job.id,
            )

    def discover(self, *, now=None, policy=POLICY):
        with sync_session() as db:
            discover_deliveries(db, policy, now or datetime.now(UTC))
            db.commit()
            return db.scalar(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.installation_id == self.installation_id
                )
            )


def make_session(owner):
    raw_refresh = uuid.uuid4().hex
    with sync_session() as db:
        session = MobileSession(
            user_id=owner,
            token_hash=refresh_token_hash(raw_refresh),
            family_id=uuid.uuid4(),
            provider="apple",
            token_version=1,
            expires_at=datetime.now(UTC) + timedelta(days=10),
        )
        db.add(session)
        db.commit()
        access, _ = issue_access_token(owner, session.id, session.token_version)
        return session.id, session.family_id, raw_refresh, {"Authorization": f"Bearer {access}"}


@pytest.fixture
def journey(monkeypatch):
    monkeypatch.setattr(settings, "mobile_jwt_secret", "ephemeral-notification-test-key")
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    _fernet.cache_clear()
    owner, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        db.get(User, owner).email = f"{owner}@example.com"
        db.commit()
    session_id, family_id, refresh, headers = make_session(owner)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        job = Job(
            user_id=owner,
            status="awaiting_device",
            raw_storage_path="test-only",
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            assembly_plan={
                "variants": [{"variant_id": "first", "render_generation_id": "approved"}]
            },
        )
        db.add(job)
        db.flush()
        thread.active_job_id = job.id
        item.current_job_id = job.id
        recipe = EditRecipeV1(
            assets=[{"id": "a", "relative_path": "a"}],
            tracks=[
                {
                    "id": "v",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "c",
                            "source_asset_id": "a",
                            "source_start": 0,
                            "source_duration": 2,
                            "timeline_start": 0,
                            "rate": 1,
                        }
                    ],
                }
            ],
        )
        request = make_device_request(job_id=job.id, variant_id="first", revision=1, recipe=recipe)
        pin_device_request(job, request, base_generation="approved")
        db.commit()
        job_id = job.id
    test_app = FastAPI()
    test_app.include_router(notification_installations.router, prefix="/me")
    test_app.include_router(native_activity.router, prefix="/me")
    test_app.include_router(auth.router, prefix="/auth")
    test_app.dependency_overrides[notification_policy] = lambda: POLICY
    with TestClient(test_app) as client:
        yield Journey(
            client,
            test_app,
            owner,
            thread_id,
            uuid.uuid4(),
            session_id,
            family_id,
            refresh,
            headers,
            job_id,
        )
    with sync_session() as db:
        db.execute(
            update(NotificationInstallation)
            .where(NotificationInstallation.owner_id == owner)
            .values(enabled=False, revoked_at=datetime.now(UTC), encrypted_token=None)
        )
        db.commit()
    _fernet.cache_clear()


def delivery_state(delivery_id):
    with sync_session() as db:
        row = db.get(NotificationDelivery, delivery_id)
        return row.status, row.attempts


def test_authenticated_registration_activity_dispatch_is_durable_and_deduplicated(journey):
    assert journey.register().json() == {
        "id": str(journey.installation_id),
        "enabled": True,
        "registration_version": 1,
    }
    assert journey.register(token=TOKEN.upper()).json()["registration_version"] == 1
    with sync_session() as db:
        installation = db.get(NotificationInstallation, journey.installation_id)
        assert installation.family_id == journey.family_id
        assert installation.encrypted_token != TOKEN.encode()
        assert decrypt_token(installation.encrypted_token) == TOKEN
    activity_id = journey.activity()
    assert (
        journey.client.get(f"/me/activities/{activity_id}", headers=journey.headers).status_code
        == 200
    )
    delivery_id = journey.discover()
    assert delivery_id is not None and journey.discover() == delivery_id
    sender = RecordingSender()
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "accepted"
    )
    assert asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "skipped"
    assert len(sender.calls) == 1
    assert sender.calls[0][1].activity_id == activity_id
    assert sender.calls[0][1].notification_id == delivery_id
    assert sender.calls[0][0].topic == "com.example.kria"
    assert delivery_state(delivery_id) == ("accepted", 1)


def test_mobile_auth_and_server_target_are_required_without_echoing_tokens(journey):
    url = f"/me/notification-installations/{journey.installation_id}"
    for headers in (
        {},
        {"Authorization": "Bearer invalid"},
        {
            "Authorization": f"Bearer {settings.internal_api_key}",
            "X-User-Id": str(journey.owner),
        },
        {**journey.headers, "X-User-Id": str(journey.owner)},
    ):
        assert journey.client.put(url, headers=headers, json={"token": TOKEN}).status_code == 401
    for body in (
        {"token": TOKEN, "topic": "com.attacker.app"},
        {"token": [TOKEN]},
        {"token": TOKEN + "secret"},
        {"token": TOKEN, "enabled": "true"},
    ):
        response = journey.client.put(url, headers=journey.headers, json=body)
        assert response.status_code == 422
        assert TOKEN not in response.text


def test_default_off_and_crypto_failure_make_no_registration(journey, monkeypatch):
    journey.app.dependency_overrides.clear()
    response = journey.client.get(
        "/me/notification-installations/capabilities", headers=journey.headers
    )
    assert response.json() == {"available": False}
    assert journey.register().status_code == 404
    assert journey.discover(policy=NotificationPolicy()) is None
    sender = RecordingSender()
    assert asyncio.run(dispatch_delivery(uuid.uuid4(), transport=sender)) == "disabled"
    assert sender.calls == []
    journey.app.dependency_overrides[notification_policy] = lambda: POLICY
    monkeypatch.setattr(settings, "token_encryption_key", "")
    _fernet.cache_clear()
    assert journey.register().status_code == 503
    with sync_session() as db:
        assert db.get(NotificationInstallation, journey.installation_id) is None


def test_installation_and_active_token_cannot_be_stolen(journey):
    assert journey.register().status_code == 200
    other, _, _ = _seed_runtime_project()
    _, _, _, other_headers = make_session(other)
    other_installation = uuid.uuid4()
    assert journey.register(headers=other_headers).status_code == 409
    assert (
        journey.register(headers=other_headers, installation_id=other_installation).status_code
        == 409
    )
    url = f"/me/notification-installations/{journey.installation_id}"
    assert journey.client.delete(url, headers=other_headers).status_code == 409
    assert journey.client.delete(url, headers=journey.headers).status_code == 204
    assert journey.client.delete(url, headers=journey.headers).status_code == 204
    assert journey.register(headers=other_headers).status_code == 409
    assert (
        journey.register(headers=other_headers, installation_id=other_installation).status_code
        == 200
    )
    assert (
        journey.client.delete(
            f"/me/notification-installations/{other_installation}", headers=other_headers
        ).status_code
        == 204
    )


@pytest.mark.parametrize(
    "change",
    [
        "read",
        "cancel",
        "generation",
        "proof",
        "publication_proof",
        "checksum",
        "artifact_pointer",
        "owner",
        "epoch",
        "quarantine",
        "preference",
        "revoke",
        "family",
        "expiry",
        "registration_expiry",
        "rotation",
    ],
)
def test_dispatch_rechecks_current_eligibility(journey, change):
    assert journey.register().status_code == 200
    activity_id = journey.activity()
    delivery_id = journey.discover()
    with sync_session() as db:
        job = db.get(Job, journey.job_id)
        item = db.get(PlanItem, job.content_plan_item_id)
        plan = db.get(ContentPlan, item.content_plan_id)
        if change == "read":
            db.get(NativeActivity, activity_id).read_at = datetime.now(UTC)
        elif change == "cancel":
            job.status = "cancelled"
        elif change in {"generation", "proof", "publication_proof", "checksum", "artifact_pointer"}:
            assembly = copy.deepcopy(job.assembly_plan)
            if change == "generation":
                assembly["variants"][0]["render_generation_id"] = "replacement"
            elif change == "proof":
                assembly["_device_render_v1"]["first"]["actual_export_policy"] = {"version": 1}
            elif change == "artifact_pointer":
                assembly["variants"][0]["video_path"] = "private/replaced.mp4"
            else:
                record = assembly["_device_render_v1"]["first"]
                attempt = record["attempts"][record["published_attempt"]]
                if change == "publication_proof":
                    attempt.pop("publication_proof")
                else:
                    attempt["publication_proof"]["sha256"] = "b" * 64
            job.assembly_plan = assembly
        elif change == "owner":
            db.get(CreationThread, journey.thread_id).active_job_id = None
        elif change == "epoch":
            plan.ownership_epoch += 1
        elif change == "quarantine":
            plan.ownership_quarantined_at = datetime.now(UTC)
        elif change == "family":
            db.execute(
                update(MobileSession)
                .where(MobileSession.family_id == journey.family_id)
                .values(revoked_at=datetime.now(UTC))
            )
        db.commit()
    if change == "preference":
        assert journey.register(enabled=False).status_code == 200
    if change == "rotation":
        assert journey.register(token="cd" * 32).status_code == 200
    if change == "revoke":
        assert (
            journey.client.delete(
                f"/me/notification-installations/{journey.installation_id}", headers=journey.headers
            ).status_code
            == 204
        )
    now = datetime.now(UTC)
    if change == "expiry":
        now += timedelta(minutes=11)
    if change == "registration_expiry":
        with sync_session() as db:
            db.get(NotificationInstallation, journey.installation_id).last_seen_at -= timedelta(
                days=31
            )
            db.commit()
    sender = RecordingSender()
    assert (
        asyncio.run(
            dispatch_delivery(delivery_id, transport=sender, policy=POLICY, clock=lambda: now)
        )
        == "skipped"
    )
    assert sender.calls == []
    assert delivery_state(delivery_id) == ("suppressed", 0)


def test_refresh_preserves_binding_and_actual_family_revoke_suppresses(journey):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    refresh = journey.client.post(
        "/auth/mobile/refresh", json={"refresh_token": journey.refresh_token}
    )
    assert refresh.status_code == 200
    new_headers = {"Authorization": "Bearer " + refresh.json()["access_token"]}
    assert journey.register(headers=new_headers).json()["registration_version"] == 1
    revoked = journey.client.post(
        "/auth/mobile/revoke", json={"refresh_token": refresh.json()["refresh_token"]}
    )
    assert revoked.status_code == 200
    assert journey.register(headers=new_headers).status_code == 401
    sender = RecordingSender()
    asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY))
    assert sender.calls == [] and delivery_state(delivery_id) == ("suppressed", 0)


@pytest.mark.parametrize(
    "result,state",
    [
        (TransportUncertain("timeout"), "uncertain"),
        (TransportUncertain("connection"), "uncertain"),
        (Rejected("provider", "InvalidProviderToken"), "rejected"),
        (Rejected("request", "BadTopic"), "rejected"),
    ],
)
def test_nonretryable_outcomes_are_terminal(journey, result, state):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    sender = RecordingSender(result)
    assert asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == state
    asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY))
    assert len(sender.calls) == 1
    assert delivery_state(delivery_id) == (state, 1)


def test_retryable_rejection_has_finite_budget_and_stable_notification_id(journey):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    sender = RecordingSender(Rejected("retryable", "RateLimited"))
    now = datetime.now(UTC)
    for attempt in range(3):
        result = asyncio.run(
            dispatch_delivery(delivery_id, transport=sender, policy=POLICY, clock=lambda: now)
        )
        assert result == ("exhausted" if attempt == 2 else "pending")
        now += timedelta(seconds=90)
    asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY, clock=lambda: now))
    assert len(sender.calls) == 3
    assert {notification.notification_id for _, notification in sender.calls} == {delivery_id}
    assert delivery_state(delivery_id) == ("exhausted", 3)


def test_old_token_rejection_does_not_revoke_rotated_registration(journey):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()

    async def rotate():
        assert journey.register(token="cd" * 32).json()["registration_version"] == 2

    sender = RecordingSender(Rejected("token", "Unregistered"), rotate)
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "rejected"
    )
    with sync_session() as db:
        installation = db.get(NotificationInstallation, journey.installation_id)
        assert installation.revoked_at is None
        assert decrypt_token(installation.encrypted_token) == "cd" * 32


def test_concurrent_dispatch_and_expired_lease_never_resend(journey):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    competitor = RecordingSender()

    async def concurrent():
        assert (
            await dispatch_delivery(delivery_id, transport=competitor, policy=POLICY) == "skipped"
        )

    sender = RecordingSender(during_send=concurrent)
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "accepted"
    )
    assert len(sender.calls) == 1 and competitor.calls == []
    journey.activity()
    with sync_session() as db:
        second_activity = db.scalar(
            select(NativeActivity.id).where(
                NativeActivity.job_id == journey.job_id,
                NativeActivity.id != sender.calls[0][1].activity_id,
            )
        )
        db.add(
            NotificationDelivery(
                id=uuid.uuid4(),
                activity_id=second_activity,
                installation_id=journey.installation_id,
                registration_version=1,
                status="leased",
                attempts=1,
                available_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                lease_id=uuid.uuid4(),
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db.commit()
        crashed = db.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.activity_id == second_activity
            )
        )
    asyncio.run(dispatch_delivery(crashed, transport=competitor, policy=POLICY))
    assert competitor.calls == [] and delivery_state(crashed) == ("uncertain", 1)


def test_disabled_task_adapters_do_not_construct_or_enqueue_transport(monkeypatch):
    from app.tasks import notification_delivery as tasks

    def forbidden():
        pytest.fail("disabled task attempted transport construction")

    monkeypatch.setattr(tasks, "notification_sender", forbidden)
    assert tasks.sweep_notifications()["status"] == "disabled"
    assert tasks.deliver_notification(str(uuid.uuid4()))["status"] == "disabled"


def test_installation_volume_is_bounded_across_distinct_current_projects(journey):
    assert journey.register().status_code == 200
    projects = [journey] + [journey.another_project() for _ in range(3)]
    activity_ids = [project.activity() for project in projects]
    journey.discover()
    with sync_session() as db:
        ids = [
            db.scalar(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.activity_id == activity_id
                )
            )
            for activity_id in activity_ids
        ]
    sender = RecordingSender()
    now = datetime.now(UTC)
    for index, delivery_id in enumerate(ids):
        asyncio.run(
            dispatch_delivery(delivery_id, transport=sender, policy=POLICY, clock=lambda: now)
        )
        now += timedelta(seconds=61)
    assert len(sender.calls) == 3
    assert delivery_state(ids[-1]) == ("pending", 0)
    now += timedelta(minutes=11)
    asyncio.run(dispatch_delivery(ids[-1], transport=sender, policy=POLICY, clock=lambda: now))
    assert delivery_state(ids[-1]) == ("suppressed", 0)
    assert len(sender.calls) == 3


def test_overlapping_projects_reserve_installation_volume_before_network_io(journey):
    assert journey.register().status_code == 200
    first_activity = journey.activity()
    other_activity = journey.another_project().activity()
    journey.discover()
    with sync_session() as db:
        first = db.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.activity_id == first_activity
            )
        )
        second = db.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.activity_id == other_activity
            )
        )
    other_sender = RecordingSender()

    async def simultaneous_send():
        assert await dispatch_delivery(second, transport=other_sender, policy=POLICY) == "skipped"

    first_sender = RecordingSender(during_send=simultaneous_send)
    assert (
        asyncio.run(dispatch_delivery(first, transport=first_sender, policy=POLICY)) == "accepted"
    )
    assert len(first_sender.calls) == 1 and other_sender.calls == []
    assert delivery_state(second) == ("pending", 0)


@pytest.mark.parametrize("reason", ["logout", "replay"])
def test_family_revocation_releases_token_but_never_transfers_installation_id(journey, reason):
    assert journey.register().status_code == 200
    if reason == "logout":
        response = journey.client.post(
            "/auth/mobile/revoke", json={"refresh_token": journey.refresh_token}
        )
        assert response.status_code == 200
    else:
        assert (
            journey.client.post(
                "/auth/mobile/refresh", json={"refresh_token": journey.refresh_token}
            ).status_code
            == 200
        )
        with sync_session() as db:
            db.get(MobileSession, journey.session_id).used_at = datetime.now(UTC) - timedelta(
                minutes=10
            )
            db.commit()
        response = journey.client.post(
            "/auth/mobile/refresh", json={"refresh_token": journey.refresh_token}
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "refresh_reuse_detected"
    other, _, _ = _seed_runtime_project()
    _, _, _, other_headers = make_session(other)
    assert journey.register(headers=other_headers).status_code == 409
    new_id = uuid.uuid4()
    assert journey.register(headers=other_headers, installation_id=new_id).status_code == 200
    assert (
        journey.client.delete(
            f"/me/notification-installations/{new_id}", headers=other_headers
        ).status_code
        == 204
    )


def test_family_revoke_rolls_back_if_installation_revocation_fails(journey):
    from app.database import engine

    assert journey.register().status_code == 200

    def fail_notification_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE notification_installations"):
            raise RuntimeError("injected registration database failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_notification_write)
    try:
        with pytest.raises(RuntimeError, match="injected registration database failure"):
            journey.client.post(
                "/auth/mobile/revoke", json={"refresh_token": journey.refresh_token}
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_notification_write)
    with sync_session() as db:
        assert db.get(MobileSession, journey.session_id).revoked_at is None
        assert db.get(NotificationInstallation, journey.installation_id).revoked_at is None


def test_expired_lease_fences_a_late_acceptance(journey):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    competitor = RecordingSender()

    async def expire_while_sending():
        later = datetime.now(UTC) + timedelta(minutes=1)
        await dispatch_delivery(
            delivery_id, transport=competitor, policy=POLICY, clock=lambda: later
        )

    sender = RecordingSender(during_send=expire_while_sending)
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "superseded"
    )
    assert delivery_state(delivery_id) == ("uncertain", 1)
    assert competitor.calls == []


def test_task_adapters_drive_durable_discovery_and_fake_delivery(journey, monkeypatch):
    from app.tasks import notification_delivery as tasks

    assert journey.register().status_code == 200
    activity_id = journey.activity()
    sender = RecordingSender()
    monkeypatch.setattr(tasks, "notification_policy", lambda: POLICY)
    monkeypatch.setattr(tasks, "notification_sender", lambda: sender)
    queued = []
    monkeypatch.setattr(
        tasks.deliver_notification, "apply_async", lambda *, args: queued.append(args[0])
    )
    assert tasks.sweep_notifications()["dispatched"] >= 1
    with sync_session() as db:
        delivery_id = db.scalar(
            select(NotificationDelivery.id).where(NotificationDelivery.activity_id == activity_id)
        )
    assert str(delivery_id) in queued
    assert tasks.deliver_notification(str(delivery_id)) == {"status": "accepted"}
    assert len(sender.calls) == 1


def test_old_invalidation_does_not_revoke_same_token_reregistered_during_send(journey):
    assert journey.register().status_code == 200
    with sync_session() as db:
        installation = db.get(NotificationInstallation, journey.installation_id)
        installation.registered_at = datetime.now(UTC) - timedelta(minutes=2)
        installation.last_seen_at = installation.registered_at
        db.commit()
    journey.activity()
    delivery_id = journey.discover()
    invalidated_at = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp() * 1000)

    async def register_same_token():
        assert journey.register().json()["registration_version"] == 1

    sender = RecordingSender(Rejected("token", "Unregistered", invalidated_at), register_same_token)
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "rejected"
    )
    with sync_session() as db:
        installation = db.get(NotificationInstallation, journey.installation_id)
        assert installation.revoked_at is None
        assert installation.enabled is True


@pytest.mark.parametrize("timestamped", [False, True])
def test_current_token_rejection_revokes_only_the_sent_registration(journey, timestamped):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()
    result = (
        Rejected("token", "Unregistered", int(datetime.now(UTC).timestamp() * 1000))
        if timestamped
        else Rejected("token", "BadDeviceToken")
    )
    sender = RecordingSender(result)
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=POLICY)) == "rejected"
    )
    with sync_session() as db:
        installation = db.get(NotificationInstallation, journey.installation_id)
        assert installation.revoked_at is not None
        assert installation.encrypted_token is None
        assert installation.registration_version == 2


@pytest.mark.parametrize("provider_timestamp", [None, "malformed"])
def test_untimestamped_invalidation_does_not_revoke_renewed_token(journey, provider_timestamp):
    assert journey.register().status_code == 200
    journey.activity()
    delivery_id = journey.discover()

    async def reject_after_renewal(request):
        assert journey.register().json()["registration_version"] == 1
        return httpx.Response(
            410,
            json={"reason": "Unregistered", "timestamp": provider_timestamp},
            extensions={"http_version": b"HTTP/2"},
        )

    async def dispatch():
        async with APNsTransport(
            lambda: SecretStr("fakeheader.fakeclaims.fakesignature"),
            transport=httpx.MockTransport(reject_after_renewal),
        ) as sender:
            return await dispatch_delivery(delivery_id, transport=sender, policy=POLICY)

    assert asyncio.run(dispatch()) == "rejected"
    with sync_session() as db:
        assert db.get(NotificationInstallation, journey.installation_id).revoked_at is None


def test_out_of_order_identical_receipts_never_regress_renewal_fence(journey):
    assert journey.register().status_code == 200
    with sync_session() as db:
        received_at = db.get(NotificationInstallation, journey.installation_id).last_seen_at
        register_installation(
            db,
            VerifiedMobilePrincipal(journey.owner, journey.family_id),
            journey.installation_id,
            SecretStr(TOKEN),
            True,
            POLICY,
            received_at - timedelta(seconds=1),
        )
        db.commit()
        assert db.get(NotificationInstallation, journey.installation_id).last_seen_at == received_at


@pytest.mark.parametrize("receipt_offset", [timedelta(0), timedelta(seconds=-1)])
def test_stale_changed_registration_cannot_overwrite_newer_receipt(journey, receipt_offset):
    assert journey.register().status_code == 200
    with sync_session() as db:
        row = db.get(NotificationInstallation, journey.installation_id)
        received_at = row.last_seen_at
        with pytest.raises(RegistrationConflict):
            register_installation(
                db,
                VerifiedMobilePrincipal(journey.owner, journey.family_id),
                journey.installation_id,
                SecretStr("cd" * 32),
                False,
                POLICY,
                received_at + receipt_offset,
            )
        db.rollback()
        row = db.get(NotificationInstallation, journey.installation_id)
        assert row.last_seen_at == received_at
        assert row.registration_version == 1 and row.enabled
        assert decrypt_token(row.encrypted_token) == TOKEN


@pytest.fixture
async def concurrent_sessions():
    engine = create_async_engine(
        settings.asyncpg_database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"statement_timeout": "10000", "lock_timeout": "10000"}},
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def wait_for_database_lock(sessions, pid):
    async with sessions() as db:
        for _ in range(500):
            waiting = await db.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
    pytest.fail("concurrent operation did not reach a real PostgreSQL lock wait")


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["logout", "replay"])
async def test_logout_waiting_for_refresh_revokes_its_uncommitted_successor(
    journey, concurrent_sessions, monkeypatch, revocation
):
    assert journey.register().status_code == 200
    inserted = asyncio.Event()
    release = asyncio.Event()
    original_issue = auth._issue_mobile_session
    replay_token = uuid.uuid4().hex
    if revocation == "replay":
        with sync_session() as db:
            db.add(
                MobileSession(
                    user_id=journey.owner,
                    family_id=journey.family_id,
                    provider="apple",
                    token_hash=refresh_token_hash(replay_token),
                    token_version=1,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    used_at=datetime.now(UTC) - timedelta(minutes=10),
                    replaced_by=journey.session_id,
                )
            )
            db.commit()

    async def paused_issue(*args, **kwargs):
        result = await original_issue(*args, **kwargs)
        inserted.set()
        await release.wait()
        return result

    monkeypatch.setattr(auth, "_issue_mobile_session", paused_issue)

    async def refresh():
        async with concurrent_sessions() as db:
            return await auth.mobile_refresh(
                auth.MobileRefreshRequest(refresh_token=journey.refresh_token), db
            )

    revoke_pid = asyncio.get_running_loop().create_future()

    async def revoke():
        async with concurrent_sessions() as db:
            revoke_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            if revocation == "replay":
                with pytest.raises(HTTPException) as error:
                    await auth.mobile_refresh(
                        auth.MobileRefreshRequest(refresh_token=replay_token), db
                    )
                assert error.value.detail == {"code": "refresh_reuse_detected"}
                return
            return await auth.mobile_revoke(
                auth.MobileRefreshRequest(refresh_token=journey.refresh_token), db
            )

    refreshing = asyncio.create_task(refresh())
    revoking = None
    try:
        await asyncio.wait_for(inserted.wait(), timeout=5)
        revoking = asyncio.create_task(revoke())
        await wait_for_database_lock(concurrent_sessions, await asyncio.wait_for(revoke_pid, 5))
    finally:
        release.set()
        refreshed = await asyncio.wait_for(refreshing, 15)
        if revoking is not None:
            await asyncio.wait_for(revoking, 15)
    with sync_session() as db:
        successor = db.scalar(
            select(MobileSession).where(
                MobileSession.token_hash == refresh_token_hash(refreshed.refresh_token)
            )
        )
        assert successor.revoked_at is not None
        assert db.get(NotificationInstallation, journey.installation_id).revoked_at is not None


@pytest.mark.asyncio
async def test_refresh_waiting_for_logout_cannot_mint_a_successor(journey, concurrent_sessions):
    assert journey.register().status_code == 200
    written = asyncio.Event()
    release = asyncio.Event()
    refresh_pid = asyncio.get_running_loop().create_future()

    async def revoke():
        async with concurrent_sessions() as db:
            original_commit = db.commit

            async def paused_commit():
                written.set()
                await release.wait()
                await original_commit()

            db.commit = paused_commit
            return await auth.mobile_revoke(
                auth.MobileRefreshRequest(refresh_token=journey.refresh_token), db
            )

    async def refresh():
        async with concurrent_sessions() as db:
            refresh_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            with pytest.raises(HTTPException) as error:
                await auth.mobile_refresh(
                    auth.MobileRefreshRequest(refresh_token=journey.refresh_token), db
                )
            assert error.value.status_code == 401

    revoking = asyncio.create_task(revoke())
    refreshing = None
    try:
        await asyncio.wait_for(written.wait(), timeout=5)
        refreshing = asyncio.create_task(refresh())
        await wait_for_database_lock(concurrent_sessions, await asyncio.wait_for(refresh_pid, 5))
    finally:
        release.set()
        await asyncio.wait_for(revoking, 15)
        if refreshing is not None:
            await asyncio.wait_for(refreshing, 15)
    with sync_session() as db:
        members = list(
            db.scalars(select(MobileSession).where(MobileSession.family_id == journey.family_id))
        )
        assert len(members) == 1 and members[0].revoked_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("first_operation", ["registration", "revoke"])
async def test_registration_and_family_revoke_serialize_in_both_orders(
    journey, concurrent_sessions, first_operation
):
    assert journey.register().status_code == 200
    written = asyncio.Event()
    release = asyncio.Event()
    waiting_pid = asyncio.get_running_loop().create_future()

    async def operation(kind, pause):
        async with concurrent_sessions() as db:
            if not pause:
                waiting_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            original_commit = db.commit

            async def paused_commit():
                written.set()
                await release.wait()
                await original_commit()

            if pause:
                db.commit = paused_commit
            if kind == "revoke":
                await auth.mobile_revoke(
                    auth.MobileRefreshRequest(refresh_token=journey.refresh_token), db
                )
                return "revoked"
            try:
                await db.run_sync(
                    lambda session: register_installation(
                        session,
                        VerifiedMobilePrincipal(journey.owner, journey.family_id),
                        journey.installation_id,
                        SecretStr("cd" * 32),
                        True,
                        POLICY,
                        datetime.now(UTC),
                    )
                )
            except RegistrationConflict:
                return "rejected"
            await db.commit()
            return "registered"

    first = asyncio.create_task(operation(first_operation, True))
    second = None
    try:
        await asyncio.wait_for(written.wait(), timeout=5)
        second_kind = "revoke" if first_operation == "registration" else "registration"
        second = asyncio.create_task(operation(second_kind, False))
        await wait_for_database_lock(concurrent_sessions, await asyncio.wait_for(waiting_pid, 5))
    finally:
        release.set()
        first_result = await asyncio.wait_for(first, 15)
        second_result = await asyncio.wait_for(second, 15) if second is not None else None
    assert (first_result, second_result) == (
        ("registered", "revoked") if first_operation == "registration" else ("revoked", "rejected")
    )
    with sync_session() as db:
        assert db.get(MobileSession, journey.session_id).revoked_at is not None
        row = db.get(NotificationInstallation, journey.installation_id)
        assert row.revoked_at is not None and row.encrypted_token is None


def test_trial_scope_hides_capability_and_registration_but_keeps_revocation(journey):
    assert journey.register().status_code == 200
    restricted = replace(POLICY, allowed_owner_ids=frozenset({uuid.uuid4()}))
    journey.app.dependency_overrides[notification_policy] = lambda: restricted
    response = journey.client.get(
        "/me/notification-installations/capabilities", headers=journey.headers
    )
    assert response.status_code == 200 and response.json() == {"available": False}
    assert journey.register().status_code == 404
    response = journey.client.delete(
        f"/me/notification-installations/{journey.installation_id}", headers=journey.headers
    )
    assert response.status_code == 204
    with sync_session() as db:
        row = db.get(NotificationInstallation, journey.installation_id)
        assert row.revoked_at is not None and row.encrypted_token is None


def test_trial_scope_removal_blocks_discovery_and_pending_admission(journey):
    assert journey.register().status_code == 200
    journey.activity()
    restricted = replace(POLICY, allowed_owner_ids=frozenset({uuid.uuid4()}))
    assert journey.discover(policy=restricted) is None
    delivery_id = journey.discover()
    assert delivery_id is not None
    sender = RecordingSender()
    assert (
        asyncio.run(dispatch_delivery(delivery_id, transport=sender, policy=restricted))
        == "skipped"
    )
    assert delivery_state(delivery_id) == ("suppressed", 0)
    assert sender.calls == []


def test_configured_runtime_drives_real_auth_registration_and_fake_http2_once(journey, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.services import apns
    from app.services.notification_runtime import notification_beat_schedule
    from app.tasks import notification_delivery as tasks

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    for name, value in {
        "native_notifications_enabled": True,
        "native_notifications_allowed_owner_ids": [journey.owner],
        "native_notifications_apns_environment": "sandbox",
        "native_notifications_apns_topic": "com.example.kria",
        "native_notifications_apns_team_id": "TEAM123456",
        "native_notifications_apns_key_id": "KEY1234567",
        "native_notifications_apns_private_key": SecretStr(pem),
    }.items():
        monkeypatch.setattr(settings, name, value)
    journey.app.dependency_overrides[notification_policy] = notification_policy
    requests, closed = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b"", extensions={"http_version": b"HTTP/2"})

    class Transport(httpx.MockTransport):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    monkeypatch.setattr(apns, "create_apns_http_transport", lambda: Transport(handle))
    queued = []
    monkeypatch.setattr(
        tasks.deliver_notification, "apply_async", lambda *, args: queued.append(args[0])
    )
    assert notification_policy().allows(journey.owner)
    assert notification_beat_schedule()
    assert journey.register().status_code == 200
    activity_id = journey.activity()
    assert tasks.sweep_notifications()["dispatched"] >= 1
    with sync_session() as db:
        delivery_id = db.scalar(
            select(NotificationDelivery.id).where(NotificationDelivery.activity_id == activity_id)
        )
    assert str(delivery_id) in queued
    assert tasks.deliver_notification(str(delivery_id)) == {"status": "accepted"}
    assert tasks.deliver_notification(str(delivery_id)) == {"status": "skipped"}
    assert len(requests) == 1 and closed == [True]
    assert str(requests[0].url) == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    assert requests[0].headers["apns-topic"] == "com.example.kria"
    assert requests[0].headers["authorization"].startswith("bearer ")
    assert delivery_state(delivery_id) == ("accepted", 1)
