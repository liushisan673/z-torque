from app.security.passwords import hash_password, verify_password
from app.security.secret_store import SecretStore


def test_password_hashing():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)


def test_secret_store_round_trip():
    store = SecretStore()
    encrypted = store.encrypt("provider-secret")
    assert encrypted != "provider-secret"
    assert store.decrypt(encrypted) == "provider-secret"
