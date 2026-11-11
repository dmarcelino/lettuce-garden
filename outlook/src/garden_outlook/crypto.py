"""Encrypt the stored Outlook session at rest.

The session (access token, refresh token, account, authority) is sealed with
AES-256-GCM under a key derived by scrypt from ``OUTLOOK_SECRET`` and a random
salt kept beside the file. The secret lives only in the environment
(compose/outlook.yml, from lettucectl), never on the data volume, so reading the
volume alone — a stray copy, a mounted backup — does not reveal the token.

Unlike the vault, there is no passphrase and no lock: the sidecar must refresh
the token unattended, so the key is available whenever the process runs. This is
encryption at rest, not an approval gate.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16
_AAD = b"garden-outlook-session-v1"

# 2^15 * 8 * 128 bytes = 32 MiB per derivation; done once at startup, not per call.
_N, _R, _P = 2**15, 8, 1


class CryptoError(Exception):
    """Wrong key, or the data was changed."""


def load_or_create_salt(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            salt = handle.read()
        if len(salt) == SALT_BYTES:
            return salt
    except FileNotFoundError:
        pass
    salt = os.urandom(SALT_BYTES)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(salt)
    os.replace(tmp, path)
    return salt


def derive_key(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        maxmem=64 * 1024 * 1024,
        dklen=KEY_BYTES,
    )


def seal(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, _AAD)


def open_sealed(key: bytes, blob: bytes) -> bytes:
    if len(blob) < NONCE_BYTES + 16:
        raise CryptoError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _AAD)
    except InvalidTag as error:
        raise CryptoError("wrong key or tampered data") from error
