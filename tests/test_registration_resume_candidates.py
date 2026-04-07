from datetime import datetime, timedelta
from pathlib import Path

from src.config.constants import OPENAI_PAGE_TYPES
from src.core.register import RegistrationEngine, RegistrationResult, SignupFormResult
from src.database import crud
from src.database.models import Base, RegistrationTask
from src.database.session import DatabaseSessionManager


def _build_engine() -> RegistrationEngine:
    engine = RegistrationEngine.__new__(RegistrationEngine)
    engine._log = lambda message, level="info": None
    engine.email = "tester@example.com"
    engine.password = None
    engine.device_id = "did-1"
    engine._is_existing_account = False
    engine._resume_attempted = False
    engine._resume_used = False
    engine._resume_source_task_uuid = ""
    engine._resume_source_state = ""
    engine._resume_candidate_invalidated = False
    engine._resume_candidate_invalidated_reason = ""
    engine._password_registered_successfully = False
    engine._username_rejected_but_login_password = False
    return engine


def test_attempt_pending_resume_login_with_saved_password_reaches_otp():
    engine = _build_engine()

    engine._submit_login_start = lambda did, sen_token: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
    )
    engine._submit_login_password = lambda: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
        is_existing_account=True,
    )

    candidate = {
        "task_uuid": "resume-task-1",
        "password": "Aa1!ResumePwd",
        "state": "pending_resume",
    }

    ok = RegistrationEngine._attempt_pending_resume_login(engine, "did-1", "sen-1", candidate)

    assert ok is True
    assert engine.password == "Aa1!ResumePwd"
    assert engine._is_existing_account is True
    assert engine._resume_used is True
    assert engine._resume_source_task_uuid == "resume-task-1"


def test_attach_resume_metadata_marks_pending_resume_after_password_submission():
    engine = _build_engine()
    engine.password = "Aa1!ResumePwd"
    engine._password_registered_successfully = True

    result = RegistrationResult(success=False, error_message="验证码校验失败", logs=[])

    enriched = RegistrationEngine._attach_resume_metadata(engine, result)

    assert enriched.password == "Aa1!ResumePwd"
    assert enriched.metadata["resume_state"] == "pending_resume"
    assert enriched.metadata["resume_reason"] == "password_submitted_but_flow_not_completed"


def test_get_latest_registration_resume_task_stops_on_newer_invalidated_record():
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_resume_candidates.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        now = datetime(2026, 4, 4, 3, 0, 0)
        session.add(
            RegistrationTask(
                task_uuid="older-pending",
                status="failed",
                created_at=now,
                result={
                    "email": "tester@example.com",
                    "password": "Aa1!OldPwd",
                    "metadata": {"resume_state": "pending_resume"},
                },
            )
        )
        session.add(
            RegistrationTask(
                task_uuid="newer-invalidated",
                status="failed",
                created_at=now + timedelta(seconds=1),
                result={
                    "email": "tester@example.com",
                    "password": "Aa1!OldPwd",
                    "metadata": {"resume_state": "invalidated"},
                },
            )
        )

    with manager.session_scope() as session:
        task = crud.get_latest_registration_resume_task(session, "tester@example.com")

    assert task is None
