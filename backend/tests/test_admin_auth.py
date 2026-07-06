from backend.admin_auth import (
    create_session_token,
    verify_admin_credentials,
    verify_session_token,
)
from backend.config import settings


def test_admin_login_credentials():
    assert verify_admin_credentials(settings.admin_username, settings.admin_password)
    assert not verify_admin_credentials(settings.admin_username, "wrong")
    assert not verify_admin_credentials("wrong", settings.admin_password)


def test_session_token_roundtrip():
    token = create_session_token(settings.admin_username)
    assert verify_session_token(token) == settings.admin_username
    assert verify_session_token("bad.token") is None
