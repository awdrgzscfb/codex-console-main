from src.web.routes import payment as payment_routes


class _DummyDb:
    def commit(self):
        return None

    def refresh(self, _account):
        return None


class _DummyCookies(dict):
    def set(self, key, value, domain=None, path=None):
        self[key] = value


class _DummySession:
    def __init__(self):
        self.cookies = _DummyCookies()


def test_bootstrap_session_token_by_relogin_resends_otp_after_initial_failure(monkeypatch):
    calls = []

    class DummyEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.callback_logger = callback_logger
            self.task_uuid = task_uuid
            self.email = None
            self.password = None
            self.email_info = None
            self.session = _DummySession()

        def _log(self, message, level="info"):
            calls.append(("log", level, message))

        def _prepare_authorize_flow(self, label):
            calls.append(("prepare", label))
            return "did-1", "sen-1"

        def _submit_login_start(self, did, sen_token):
            calls.append(("login_start", did, sen_token))
            return type("Result", (), {"success": True, "page_type": payment_routes.OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]})()

        def _submit_login_password(self):
            calls.append(("login_password",))
            return type(
                "Result",
                (),
                {"success": True, "is_existing_account": True, "page_type": payment_routes.OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"], "error_message": ""},
            )()

        def _verify_email_otp_with_retry(self, stage_label="验证码", max_attempts=3, fetch_timeout=None, attempted_codes=None):
            calls.append(("verify", stage_label, max_attempts, fetch_timeout))
            return stage_label == "会话补全验证码(原地重发)"

        def _send_verification_code(self, referer=None):
            calls.append(("send_otp", referer))
            return True

        def _retrigger_login_otp(self):
            calls.append(("retrigger",))
            return True

        def _dump_session_cookies(self):
            return "__Secure-next-auth.session-token=session-123"

    monkeypatch.setattr(payment_routes, "RegistrationEngine", DummyEngine)
    monkeypatch.setattr(payment_routes, "_resolve_email_service_for_account_session_bootstrap", lambda db, account, proxy: object())
    monkeypatch.setattr(payment_routes, "_force_fetch_nextauth_session_token", lambda **kwargs: ("", ""))

    account = type(
        "Account",
        (),
        {
            "id": 1,
            "email": "tester@example.com",
            "password": "Aa1!Pwd",
            "email_service": "outlook",
            "email_service_id": "tester@example.com",
            "access_token": "",
            "cookies": "",
            "session_token": "",
        },
    )()

    token = payment_routes._bootstrap_session_token_by_relogin(_DummyDb(), account, proxy=None)

    assert token == "session-123"
    assert ("verify", "会话补全验证码", 1, 120) in calls
    assert ("send_otp", "https://auth.openai.com/email-verification") in calls
    assert ("verify", "会话补全验证码(原地重发)", 1, 120) in calls


def test_bootstrap_session_token_by_relogin_stops_when_retrigger_login_otp_fails(monkeypatch):
    calls = []

    class DummyEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.session = _DummySession()
            self.email = None
            self.password = None
            self.email_info = None

        def _log(self, message, level="info"):
            calls.append(("log", level, message))

        def _prepare_authorize_flow(self, label):
            return "did-1", "sen-1"

        def _submit_login_start(self, did, sen_token):
            return type("Result", (), {"success": True, "page_type": payment_routes.OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]})()

        def _submit_login_password(self):
            return type(
                "Result",
                (),
                {"success": True, "is_existing_account": True, "page_type": payment_routes.OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"], "error_message": ""},
            )()

        def _verify_email_otp_with_retry(self, stage_label="验证码", max_attempts=3, fetch_timeout=None, attempted_codes=None):
            calls.append(("verify", stage_label, max_attempts, fetch_timeout))
            return False

        def _send_verification_code(self, referer=None):
            calls.append(("send_otp", referer))
            return True

        def _retrigger_login_otp(self):
            calls.append(("retrigger",))
            return False

    monkeypatch.setattr(payment_routes, "RegistrationEngine", DummyEngine)
    monkeypatch.setattr(payment_routes, "_resolve_email_service_for_account_session_bootstrap", lambda db, account, proxy: object())

    account = type(
        "Account",
        (),
        {
            "id": 2,
            "email": "tester@example.com",
            "password": "Aa1!Pwd",
            "email_service": "outlook",
            "email_service_id": "tester@example.com",
            "access_token": "",
            "cookies": "",
            "session_token": "",
        },
    )()

    token = payment_routes._bootstrap_session_token_by_relogin(_DummyDb(), account, proxy=None)

    assert token == ""
    assert ("retrigger",) in calls
    assert ("verify", "会话补全验证码(重发)", 3, 120) not in calls
