"""Argon2 密码哈希的纯逻辑测试：不连接数据库。"""

from rag_backend.auth.passwords import (
    dummy_password_hash,
    hash_password,
    verify_password,
    verify_password_or_dummy,
)

PASSWORD = "correct horse battery staple"


def test_hash_password_round_trips() -> None:
    password_hash = hash_password(PASSWORD)

    assert password_hash.startswith("$argon2")
    assert verify_password(password_hash, PASSWORD) is True


def test_hash_password_does_not_contain_plaintext() -> None:
    password_hash = hash_password(PASSWORD)

    assert PASSWORD not in password_hash


def test_hash_password_uses_a_fresh_salt() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second


def test_verify_password_rejects_wrong_password() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password(password_hash, "wrong") is False


def test_verify_password_rejects_invalid_hash() -> None:
    assert verify_password("not-a-hash", PASSWORD) is False


def test_hash_password_rejects_empty_password() -> None:
    try:
        hash_password("")
    except ValueError as error:
        assert "密码" in str(error)
    else:  # pragma: no cover - 不应到达
        raise AssertionError("空密码应被拒绝")


def test_verify_password_or_dummy_returns_false_for_missing_user() -> None:
    assert verify_password_or_dummy(None, PASSWORD) is False
    # 占位哈希也必须是一次真实 Argon2 值。
    assert dummy_password_hash().startswith("$argon2")


def test_verify_password_or_dummy_checks_real_hash_when_present() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password_or_dummy(password_hash, PASSWORD) is True
    assert verify_password_or_dummy(password_hash, "wrong") is False
