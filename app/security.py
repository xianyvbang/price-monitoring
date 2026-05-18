from __future__ import annotations

import base64
import hashlib
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(value: Optional[str], secret_key: str) -> Optional[str]:
    if value is None or value == "":
        return None
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(value: Optional[str], secret_key: str) -> Optional[str]:
    if value is None or value == "":
        return None
    return _fernet(secret_key).decrypt(value.encode("utf-8")).decode("utf-8")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False
