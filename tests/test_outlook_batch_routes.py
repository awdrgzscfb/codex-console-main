import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, EmailService, Account
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def test_resolve_effective_email_service_id_falls_back_to_task_binding(monkeypatch):
    task = type("Task", (), {"email_service_id": 42})()

    monkeypatch.setattr(
        registration_routes.crud,
        "get_registration_task",
        lambda db, task_uuid: task,
    )

    assert registration_routes._resolve_effective_email_service_id(object(), "task-1", None) == 42
    assert registration_routes._resolve_effective_email_service_id(object(), "task-1", 7) == 7


def test_run_outlook_batch_registration_forwards_registration_type(monkeypatch):
    created = []
    captured = {}

    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(registration_routes, "get_db", lambda: DummyDb())
    monkeypatch.setattr(
        registration_routes.crud,
        "create_registration_task",
        lambda db, task_uuid, proxy=None, email_service_id=None: created.append(
            {"task_uuid": task_uuid, "proxy": proxy, "email_service_id": email_service_id}
        ),
    )

    async def fake_run_batch_registration(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(registration_routes, "run_batch_registration", fake_run_batch_registration)

    asyncio.run(
        registration_routes.run_outlook_batch_registration(
            batch_id="batch-1",
            service_ids=[11, 12],
            skip_registered=True,
            proxy="http://proxy.local:7890",
            interval_min=1,
            interval_max=2,
            concurrency=3,
            mode="parallel",
            registration_type="parent",
        )
    )

    assert [item["email_service_id"] for item in created] == [11, 12]
    assert captured["batch_id"] == "batch-1"
    assert captured["email_service_type"] == "outlook"
    assert captured["email_service_id"] is None
    assert captured["registration_type"] == "parent"


def test_get_outlook_accounts_for_registration_matches_registered_email_case_insensitively(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "outlook_case_status.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="outlook",
                name="MixedCase@Outlook.com",
                config={"email": "MixedCase@Outlook.com", "password": "secret"},
                enabled=True,
                priority=0,
            )
        )
        session.add(
            Account(
                email="mixedcase@outlook.com",
                email_service="outlook",
                status="active",
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    result = asyncio.run(registration_routes.get_outlook_accounts_for_registration())

    assert result.total == 1
    assert result.registered_count == 1
    assert result.unregistered_count == 0
    assert result.accounts[0].is_registered is True
    assert result.accounts[0].email == "MixedCase@Outlook.com"
