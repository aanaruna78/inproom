"""INPROOM-108: credential vault - AES-256-GCM, tamper detection, key rotation, redaction."""

import base64
import os
import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.connectors import vault
from app.connectors.vault import Secret, VaultError
from app.db import tenant_session

AAD = b"inproom:t:c:credential"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def keys(monkeypatch):
    def use(current: str, key_id: str = "k1", old: str = "", env: str = "local"):
        monkeypatch.setenv("INPROOM_SECRETS_KEY", current)
        monkeypatch.setenv("INPROOM_SECRETS_KEY_ID", key_id)
        monkeypatch.setenv("INPROOM_SECRETS_OLD_KEYS", old)
        monkeypatch.setenv("INPROOM_ENV", env)
        get_settings.cache_clear()

    return use


def test_round_trip_and_fresh_nonce(keys):
    keys(_key())
    a = vault.encrypt(Secret("ghp_supersecret"), AAD)
    b = vault.encrypt(Secret("ghp_supersecret"), AAD)
    assert a[1] != b[1] and a[2] != b[2]  # never the same nonce/ciphertext twice
    assert b"ghp_supersecret" not in a[2]
    assert vault.decrypt(*a, AAD).reveal() == "ghp_supersecret"


@pytest.mark.parametrize("part", ["ciphertext", "nonce", "aad"])
def test_tampering_is_detected(keys, part):
    keys(_key())
    key_id, nonce, ct = vault.encrypt(Secret("ghp_supersecret"), AAD)
    aad = AAD
    if part == "ciphertext":
        ct = bytes([ct[0] ^ 1]) + ct[1:]
    elif part == "nonce":
        nonce = bytes([nonce[0] ^ 1]) + nonce[1:]
    else:
        aad = b"inproom:other-tenant:c:credential"  # ciphertext moved to another row/tenant
    with pytest.raises(VaultError):
        vault.decrypt(key_id, nonce, ct, aad)


def test_rotation_old_key_still_decrypts(keys):
    old = _key()
    keys(old, key_id="k1")
    sealed = vault.encrypt(Secret("ghp_rotate_me"), AAD)
    keys(_key(), key_id="k2", old=f"k1={old}")
    assert vault.decrypt(*sealed, AAD).reveal() == "ghp_rotate_me"
    assert vault.encrypt(Secret("x" * 10), AAD)[0] == "k2"
    keys(_key(), key_id="k3")  # k1 dropped entirely
    with pytest.raises(VaultError, match="unknown key id"):
        vault.decrypt(*sealed, AAD)


def test_secret_never_prints_its_value():
    s = Secret("ghp_abcdefghijklmnop")
    assert "ghp_" not in str(s) and "ghp_" not in repr(s) and "ghp_" not in f"{s} {[s]} {dict(t=s)}"
    assert s.fingerprint() == "…mnop" and Secret("short").fingerprint() == "…"


def test_key_is_mandatory_outside_local(monkeypatch):
    monkeypatch.delenv("INPROOM_SECRETS_KEY", raising=False)
    monkeypatch.setenv("INPROOM_ENV", "production")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="required"):
        vault.keyring()


def test_bad_key_length_is_rejected(keys):
    keys(base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="32 bytes"):
        vault.keyring()


@pytest.mark.integration
def test_store_load_rotates_on_read_and_destroy(keys, owner_engine, make_tenant):
    tid, cid = make_tenant(), uuid.uuid4()
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO source_connections (id, tenant_id, provider, kind, display_name, base_url, "
                "auth_type, status, created_by) VALUES (:c, :t, 'github', 'git', 'gh', "
                "'https://api.github.com', 'token', 'ACTIVE', 'seed')"
            ),
            {"c": cid, "t": tid},
        )
    old = _key()
    keys(old, key_id="k1")
    with tenant_session(tid) as conn:
        vault.store(conn, tid, cid, "credential", Secret("ghp_stored_token_1234"))
    with owner_engine.connect() as conn:
        raw = conn.execute(
            text("SELECT key_id, ciphertext FROM connection_secrets WHERE connection_id=:c"), {"c": cid}
        ).one()
    assert raw.key_id == "k1" and b"ghp_stored" not in bytes(raw.ciphertext)
    keys(_key(), key_id="k2", old=f"k1={old}")
    with tenant_session(tid) as conn:
        assert vault.load(conn, tid, cid, "credential").reveal() == "ghp_stored_token_1234"
    with owner_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT key_id FROM connection_secrets WHERE connection_id=:c"), {"c": cid}
            ).scalar()
            == "k2"
        )
    other = make_tenant()
    with tenant_session(other) as conn:  # RLS: another tenant cannot even see the row
        assert vault.load(conn, other, cid, "credential") is None
    with tenant_session(tid) as conn:
        assert vault.destroy(conn, tid, cid) == 1
        assert vault.load(conn, tid, cid, "credential") is None
