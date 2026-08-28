from cryptography.fernet import Fernet

from app.core.config import get_settings

settings = get_settings()
_fernet = Fernet(settings.credential_encryption_key.encode())


def encrypt_payload(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_payload(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
