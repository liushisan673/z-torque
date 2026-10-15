from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretStoreError(Exception):
    pass


class SecretStore:
    def __init__(self) -> None:
        key = get_settings().secret_store_key.get_secret_value()
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise SecretStoreError("invalid secret store key") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise SecretStoreError("secret decryption failed") from exc
