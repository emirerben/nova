from __future__ import annotations

from app.services.reviewer_login import DUMMY_HASH, hash_password, verify_password


def test_round_trip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True


def test_wrong_password_rejected() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("wrong password", encoded) is False


def test_malformed_string_never_raises() -> None:
    for garbage in ["", "not-a-hash", "scrypt$1$2", "bcrypt$32768$8$1$aa$bb", "a$b$c$d$e$f"]:
        assert verify_password("anything", garbage) is False


def test_tampered_params_rejected() -> None:
    encoded = hash_password("correct horse battery staple")
    algorithm, n, r, p, salt, digest = encoded.split("$")
    tampered = "$".join([algorithm, "999999999999", r, p, salt, digest])
    assert verify_password("correct horse battery staple", tampered) is False


def test_out_of_bounds_cost_params_rejected() -> None:
    encoded = hash_password("correct horse battery staple")
    algorithm, _n, r, p, salt, digest = encoded.split("$")
    huge_n = "$".join([algorithm, str(2**30), r, p, salt, digest])
    assert verify_password("correct horse battery staple", huge_n) is False


def test_dummy_hash_is_valid_and_never_matches_real_passwords() -> None:
    assert verify_password("password", DUMMY_HASH) is False
    assert verify_password("", DUMMY_HASH) is False
