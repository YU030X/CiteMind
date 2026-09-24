"""会话令牌与 CSRF 派生的纯逻辑测试。"""

from pydantic import SecretStr
from rag_backend.auth.tokens import (
    CSRF_HEADER_NAME,
    SESSION_TOKEN_BYTES,
    csrf_token_hash_matches,
    csrf_tokens_match,
    derive_csrf_from_secret,
    derive_csrf_token,
    generate_session_token,
    hash_token,
    session_cookie_settings,
)

SECRET = "unit-test-csrf-secret"


def test_session_token_is_high_entropy_and_unique() -> None:
    tokens = {generate_session_token() for _ in range(64)}

    assert len(tokens) == 64
    for token in tokens:
        # token_urlsafe(32) 至少编码 32 字节随机量。
        assert len(token) >= 43
    assert len(generate_session_token()) > SESSION_TOKEN_BYTES


def test_hash_token_is_deterministic_and_irreversible_length() -> None:
    token = generate_session_token()

    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64


def test_derive_csrf_token_is_deterministic_and_secret_dependent() -> None:
    token = generate_session_token()

    derived = derive_csrf_token(SECRET, token)

    assert derived == derive_csrf_token(SECRET, token)
    assert derived != derive_csrf_token("another-secret", token)
    assert derived != derive_csrf_token(SECRET, generate_session_token())


def test_derive_csrf_from_secret_accepts_secret_str() -> None:
    token = generate_session_token()

    assert derive_csrf_from_secret(SecretStr(SECRET), token) == derive_csrf_token(
        SECRET, token
    )


def test_csrf_token_hash_matches_only_for_matching_secret_and_token() -> None:
    token = generate_session_token()
    stored_hash = hash_token(derive_csrf_token(SECRET, token))

    assert csrf_token_hash_matches(SECRET, token, stored_hash) is True
    assert csrf_token_hash_matches(SECRET, generate_session_token(), stored_hash) is False
    assert csrf_token_hash_matches("other", token, stored_hash) is False


def test_csrf_tokens_match_handles_missing_and_mismatch() -> None:
    assert csrf_tokens_match("abc", "abc") is True
    assert csrf_tokens_match("abc", "abd") is False
    assert csrf_tokens_match("abc", None) is False


def test_session_cookie_settings_are_hardened() -> None:
    settings = session_cookie_settings(secure=True, max_age=3600)

    assert settings["httponly"] is True
    assert settings["secure"] is True
    assert settings["samesite"] == "lax"
    assert settings["path"] == "/"
    assert settings["max_age"] == 3600
    assert CSRF_HEADER_NAME == "X-CSRF-Token"
