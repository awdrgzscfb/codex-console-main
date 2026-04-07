from src.core.openai.token_refresh import TokenRefreshManager


class _DummyResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _DummySession:
    def __init__(self, response):
        self.response = response

    def post(self, url, headers=None, data=None, timeout=None):
        return self.response


def test_refresh_by_oauth_token_reports_rotated_refresh_token(monkeypatch):
    manager = TokenRefreshManager.__new__(TokenRefreshManager)
    manager.proxy_url = None
    manager.settings = type("Settings", (), {"openai_client_id": "client-1", "openai_redirect_uri": "http://localhost"})()
    manager._create_session = lambda: _DummySession(
        _DummyResponse(
            status_code=401,
            payload={
                "error": {
                    "message": "Your refresh token has already been used to generate a new access token. Please try signing in again.",
                    "code": "invalid_grant",
                }
            },
            text='{"error":{"message":"used","code":"invalid_grant"}}',
        )
    )

    result = TokenRefreshManager.refresh_by_oauth_token(manager, "rt-old", "client-1")

    assert result.success is False
    assert "之前已经被用来换过新 token" in result.error_message


def test_refresh_account_preserves_message_when_access_token_still_valid(monkeypatch):
    manager = TokenRefreshManager.__new__(TokenRefreshManager)
    manager.proxy_url = None
    manager.settings = type("Settings", (), {"openai_client_id": "client-1", "openai_redirect_uri": "http://localhost"})()

    manager.refresh_by_oauth_token = lambda refresh_token, client_id=None: type(
        "RefreshResult",
        (),
        {
            "success": False,
            "error_message": (
                "OAuth Refresh Token 已失效：这个 refresh_token 之前已经被用来换过新 token，"
                "当前数据库里保存的是旧值，需要重新登录补全最新 refresh_token"
            ),
        },
    )()
    manager.validate_token = lambda access_token, timeout_seconds=15: (True, None)

    account = type(
        "Account",
        (),
        {
            "email": "tester@example.com",
            "session_token": "",
            "cookies": "",
            "refresh_token": "rt-old",
            "access_token": "at-still-valid",
            "client_id": "client-1",
        },
    )()

    result = TokenRefreshManager.refresh_account(manager, account)

    assert result.success is False
    assert "当前 access_token 仍然可用" in result.error_message
