"""Optional encryption of backup files at rest (R18).

Backups hold customer personal data. When the secret ``BACKUP_PASSPHRASE`` is set in ``.env``, every database file in a
new backup is written encrypted (``<name>.enc``) and the plain copy is deleted. The passphrase is never stored in a
backup or in the database, so keep it in a password manager: a lost passphrase means the encrypted backups cannot be
read. File format: ``MAGIC | salt (16) | nonce (12) | AES-256-GCM ciphertext+tag``, key = scrypt(passphrase, salt).
The whole file is processed in memory, which is fine for the database sizes this project targets.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"AECOMBK1"
SUFFIX = ".enc"
SALT_LEN, NONCE_LEN = 16, 12
MIN_PASSPHRASE = 12


class CryptoError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def check_passphrase(passphrase: str | None) -> str:
    if not passphrase or len(passphrase) < MIN_PASSPHRASE:
        raise CryptoError(f"BACKUP_PASSPHRASE must be at least {MIN_PASSPHRASE} characters")
    return passphrase


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    check_passphrase(passphrase)
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    data = AESGCM(_key(passphrase, salt)).encrypt(nonce, Path(src).read_bytes(), MAGIC)
    tmp = Path(dst).with_name(Path(dst).name + ".part")
    tmp.write_bytes(MAGIC + salt + nonce + data)
    tmp.replace(dst)


def decrypt_file(src: Path, dst: Path, passphrase: str | None) -> None:
    if not passphrase:
        raise CryptoError("this backup is encrypted: set BACKUP_PASSPHRASE in .env (the one used when it was made)")
    raw = Path(src).read_bytes()
    head = len(MAGIC) + SALT_LEN + NONCE_LEN
    if len(raw) <= head or not raw.startswith(MAGIC):
        raise CryptoError(f"{Path(src).name}: not an aecom encrypted backup file")
    salt = raw[len(MAGIC):len(MAGIC) + SALT_LEN]
    nonce = raw[len(MAGIC) + SALT_LEN:head]
    try:
        plain = AESGCM(_key(passphrase, salt)).decrypt(nonce, raw[head:], MAGIC)
    except InvalidTag as e:
        raise CryptoError(f"{Path(src).name}: wrong BACKUP_PASSPHRASE or the file is damaged") from e
    Path(dst).write_bytes(plain)
