import base64
import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def make_fernet(key: bytes) -> Fernet:
    return Fernet(key)


def encrypt(f: Fernet, plaintext: str) -> bytes:
    return f.encrypt(plaintext.encode())


def decrypt(f: Fernet, ciphertext: bytes) -> str:
    return f.decrypt(ciphertext).decode()
