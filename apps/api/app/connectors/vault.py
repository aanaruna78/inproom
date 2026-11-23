"""Credential vault (INPROOM-108): AES-256-GCM encryption of connector secrets.

- A fresh 96-bit nonce per encryption; the tenant, connection and purpose are bound as associated data, so a
  ciphertext copied to another row (or tenant) fails to decrypt.
- Keys are identified by key_id. The current key encrypts; old keys (INPROOM_SECRETS_OLD_KEYS) only decrypt,
  and a secret read with an old key is re-encrypted with the current one (rotation on read).
- Plaintext only ever lives in a `Secret`, whose str/repr are redacted, so it cannot slip into logs, audit
  payloads or error messages by accident. INPROOM-38 (BYOK/KMS) replaces the key provider, not this API.
"""

import base64
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db_schema import connection_secrets

log = logging.getLogger(__name__)

# Deterministic development key, used ONLY when INPROOM_ENV=local and no key is configured.
_LOCAL_DEV_KEY = hashlib.sha256(b"inproom-local-dev-only-secrets-key").digest()


class VaultError(Exception):
    """The secret could not be decrypted (wrong key, tampered data or moved ciphertext)."""


@dataclass(frozen=True)
class Secret:
    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def fingerprint(self) -> str:
        return "…" + self._value[-4:] if len(self._value) >= 8 else "…"

    def __str__(self) -> str:
        return "Secret(****)"

    __repr__ = __str__


def _decode_key(raw: str) -> bytes:
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError("secrets keys must be 32 bytes (base64)")
    return key


def keyring() -> tuple[str, dict[str, bytes]]:
    """(current key id, {key id: key}) from configuration."""
    s = get_settings()
    keys: dict[str, bytes] = {}
    for item in filter(None, (p.strip() for p in s.secrets_old_keys.split(","))):
        key_id, _, raw = item.partition("=")
        keys[key_id] = _decode_key(raw)
    if s.secrets_key:
        keys[s.secrets_key_id] = _decode_key(s.secrets_key)
    elif s.env == "local":
        log.warning("INPROOM_SECRETS_KEY not set: using the local development key (never use outside local)")
        keys[s.secrets_key_id] = _LOCAL_DEV_KEY
    else:
        raise RuntimeError("INPROOM_SECRETS_KEY is required outside the local environment")
    return s.secrets_key_id, keys


def _aad(tenant_id: uuid.UUID, connection_id: uuid.UUID, purpose: str) -> bytes:
    return f"inproom:{tenant_id}:{connection_id}:{purpose}".encode()


def encrypt(secret: Secret, aad: bytes) -> tuple[str, bytes, bytes]:
    key_id, keys = keyring()
    nonce = os.urandom(12)
    return key_id, nonce, AESGCM(keys[key_id]).encrypt(nonce, secret.reveal().encode(), aad)


def decrypt(key_id: str, nonce: bytes, ciphertext: bytes, aad: bytes) -> Secret:
    _, keys = keyring()
    key = keys.get(key_id)
    if key is None:
        raise VaultError(f"unknown key id {key_id!r}")
    try:
        return Secret(AESGCM(key).decrypt(nonce, ciphertext, aad).decode())
    except InvalidTag as exc:
        raise VaultError("secret failed integrity check") from exc


def store(
    conn: Connection, tenant_id: uuid.UUID, connection_id: uuid.UUID, purpose: str, secret: Secret
) -> None:
    key_id, nonce, ciphertext = encrypt(secret, _aad(tenant_id, connection_id, purpose))
    stmt = pg_insert(connection_secrets).values(
        tenant_id=tenant_id,
        connection_id=connection_id,
        purpose=purpose,
        key_id=key_id,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["connection_id", "purpose"],
            set_={"key_id": key_id, "nonce": nonce, "ciphertext": ciphertext},
        )
    )


def load(conn: Connection, tenant_id: uuid.UUID, connection_id: uuid.UUID, purpose: str) -> Secret | None:
    row = (
        conn.execute(
            select(connection_secrets).where(
                connection_secrets.c.tenant_id == tenant_id,
                connection_secrets.c.connection_id == connection_id,
                connection_secrets.c.purpose == purpose,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    aad = _aad(tenant_id, connection_id, purpose)
    secret = decrypt(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]), aad)
    current_id, _ = keyring()
    if row["key_id"] != current_id:  # rotation on read
        key_id, nonce, ciphertext = encrypt(secret, aad)
        conn.execute(
            update(connection_secrets)
            .where(
                connection_secrets.c.tenant_id == tenant_id,
                connection_secrets.c.connection_id == connection_id,
                connection_secrets.c.purpose == purpose,
            )
            .values(key_id=key_id, nonce=nonce, ciphertext=ciphertext)
        )
    return secret


def destroy(conn: Connection, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> int:
    result = conn.execute(
        delete(connection_secrets).where(
            connection_secrets.c.tenant_id == tenant_id, connection_secrets.c.connection_id == connection_id
        )
    )
    return result.rowcount
