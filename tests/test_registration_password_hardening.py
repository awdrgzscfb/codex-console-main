from src.config.constants import OPENAI_PAGE_TYPES, PASSWORD_SPECIAL_CHARSET
from src.core import register as register_module
from src.core.anyauto.register_flow import AnyAutoRegistrationEngine
from src.core.register import RegistrationEngine
from src.core.utils import generate_password


def _assert_password_is_hardened(password: str) -> None:
    assert len(password) >= 8
    assert any(ch.islower() for ch in password)
    assert any(ch.isupper() for ch in password)
    assert any(ch.isdigit() for ch in password)
    assert any(ch in PASSWORD_SPECIAL_CHARSET for ch in password)


def test_generate_password_contains_special_characters():
    _assert_password_is_hardened(generate_password(12))


def test_registration_engine_generate_password_contains_special_characters():
    engine = RegistrationEngine.__new__(RegistrationEngine)
    _assert_password_is_hardened(RegistrationEngine._generate_password(engine, 12))


def test_anyauto_generate_password_contains_special_characters():
    _assert_password_is_hardened(AnyAutoRegistrationEngine._build_password(12))


def test_register_password_with_retry_retries_generic_400(monkeypatch):
    engine = RegistrationEngine.__new__(RegistrationEngine)
    attempts = []
    logs = []

    def fake_register_password(_did=None, _sen_token=None):
        attempts.append(1)
        if len(attempts) < 3:
            engine._last_register_password_error = "注册密码接口返回异常: Failed to create account. Please try again."
            return False, None
        return True, "Aa1!retryPwd"

    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)
    engine._register_password = fake_register_password
    engine._last_register_password_error = None
    engine._log = lambda message, level="info": logs.append((level, message))

    success, password = RegistrationEngine._register_password_with_retry(engine, None, None)

    assert success is True
    assert password == "Aa1!retryPwd"
    assert len(attempts) == 3
    assert any("可重试 400" in message for _level, message in logs)


class _ErrorResponse:
    def __init__(self, message: str, code: str = "bad_request"):
        self.status_code = 400
        self.text = message
        self._payload = {"error": {"message": message, "code": code}}

    def json(self):
        return self._payload


class _SingleResponseSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        return self.response


def test_register_password_username_probe_login_password_does_not_mark_registered():
    engine = RegistrationEngine.__new__(RegistrationEngine)
    logs = []
    marked = []

    engine.email = "tester@example.com"
    engine.password = None
    engine.session = _SingleResponseSession(
        _ErrorResponse("Failed to register username. Please try again.")
    )
    engine._generate_password = lambda length=12: "Aa1!ProbePwd"
    engine._log = lambda message, level="info": logs.append((level, message))
    engine._submit_login_start = lambda did, sen_token: type(
        "ProbeResult",
        (),
        {"success": True, "page_type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]},
    )()
    engine._mark_email_as_registered = lambda: marked.append(True)

    success, password = RegistrationEngine._register_password(engine, "did-1", "token-1")

    assert success is False
    assert password is None
    assert marked == []
    assert "本轮不会将其标记为已注册" in engine._last_register_password_error
    assert any("暂不标记为已注册" in message for _level, message in logs)


def test_register_password_username_probe_email_otp_still_marks_registered():
    engine = RegistrationEngine.__new__(RegistrationEngine)
    marked = []

    engine.email = "tester@example.com"
    engine.password = None
    engine.session = _SingleResponseSession(
        _ErrorResponse("Failed to register username. Please try again.")
    )
    engine._generate_password = lambda length=12: "Aa1!ProbePwd"
    engine._log = lambda message, level="info": None
    engine._submit_login_start = lambda did, sen_token: type(
        "ProbeResult",
        (),
        {"success": True, "page_type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
    )()
    engine._mark_email_as_registered = lambda: marked.append(True)

    success, password = RegistrationEngine._register_password(engine, "did-1", "token-1")

    assert success is False
    assert password is None
    assert marked == [True]
    assert "该邮箱已存在 OpenAI 账号" in engine._last_register_password_error
