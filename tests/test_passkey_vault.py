"""The passkey vault: assertion verification, challenges, and the HTTP contract.

Storage is mocked — the repo functions are thin — and every assertion is made
by a real key, built the way an authenticator builds one, so the verifier is
exercised end to end rather than against a stub.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from app.api import app as fastapi_app
from app.core.database import get_db
from app.routers.passkey_vault.router import (
    rate_limit_passkey_challenge,
    rate_limit_passkey_open,
)
from app.services import passkey_vault_service as service
from app.utils.webauthn import (
    ALG_EDDSA,
    ALG_ES256,
    ALG_RS256,
    AssertionInvalid,
    verify_assertion,
)

ORIGIN = "http://localhost:3000"  # FRONTEND_URL in the test env
RP_ID = "localhost"
CREDENTIAL_ID = base64.urlsafe_b64encode(b"credential-0001").rstrip(b"=").decode()
CREDENTIAL_HASH = hashlib.sha256(b"credential-0001").hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Authenticator:
    """Signs assertions the way a platform authenticator does."""

    def __init__(self, alg: int = ALG_ES256) -> None:
        self.alg = alg
        if alg == ALG_ES256:
            self.key = ec.generate_private_key(ec.SECP256R1())
        elif alg == ALG_RS256:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            self.key = ed25519.Ed25519PrivateKey.generate()

    @property
    def spki(self) -> str:
        return b64url(
            self.key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    def assert_(
        self,
        challenge: str,
        *,
        origin: str = ORIGIN,
        rp_id: str = RP_ID,
        flags: int = 0x05,
        sign_count: int = 0,
        type_: str = "webauthn.get",
    ) -> dict:
        client_data = json.dumps(
            {"type": type_, "challenge": challenge, "origin": origin}
        ).encode()
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + sign_count.to_bytes(4, "big")
        )
        signed = auth_data + hashlib.sha256(client_data).digest()
        if self.alg == ALG_ES256:
            signature = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        elif self.alg == ALG_RS256:
            signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            signature = self.key.sign(signed)
        return {
            "credentialId": CREDENTIAL_ID,
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(signature),
        }


def verify(authenticator: Authenticator, assertion: dict, **overrides):
    return verify_assertion(
        client_data_json=assertion["clientDataJSON"],
        authenticator_data=assertion["authenticatorData"],
        signature=assertion["signature"],
        public_key=overrides.get("public_key", authenticator.spki),
        alg=overrides.get("alg", authenticator.alg),
        allowed_origins=overrides.get("origins", [ORIGIN]),
    )


class SpendOnce:
    """Redis SET NX: the first spend of a nonce wins."""

    def __init__(self) -> None:
        self.spent: set[str] = set()

    async def set(self, key, value, nx=False, ex=None):
        if key in self.spent:
            return None
        self.spent.add(key)
        return True


@pytest.fixture
def redis(monkeypatch) -> SpendOnce:
    fake = SpendOnce()
    monkeypatch.setattr(service, "get_redis_client", lambda: fake)
    return fake


# --------------------------------------------------------------------------
# verify_assertion
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alg", [ALG_ES256, ALG_RS256, ALG_EDDSA])
def test_an_assertion_from_the_stored_key_verifies(alg):
    authenticator = Authenticator(alg)
    verified = verify(authenticator, authenticator.assert_("abc", sign_count=7))
    assert verified.challenge == "abc"
    assert verified.sign_count == 7


def test_another_key_cannot_sign_for_the_stored_one():
    with pytest.raises(AssertionInvalid):
        verify(
            Authenticator(),
            Authenticator().assert_("abc"),
            public_key=Authenticator().spki,
        )


def test_a_key_of_another_type_than_its_alg_is_refused():
    authenticator = Authenticator(ALG_ES256)
    with pytest.raises(AssertionInvalid):
        verify(authenticator, authenticator.assert_("abc"), alg=ALG_RS256)


@pytest.mark.parametrize(
    "tamper",
    [
        {"origin": "https://evil.example"},
        {"rp_id": "evil.example"},
        {"flags": 0x01},  # present, not verified
        {"type_": "webauthn.create"},
    ],
)
def test_assertions_from_elsewhere_or_unverified_are_refused(tamper):
    authenticator = Authenticator()
    with pytest.raises(AssertionInvalid):
        verify(authenticator, authenticator.assert_("abc", **tamper))


def test_a_signature_over_other_client_data_is_refused():
    authenticator = Authenticator()
    good = authenticator.assert_("abc")
    other = authenticator.assert_("xyz")
    with pytest.raises(AssertionInvalid):
        verify(authenticator, {**good, "signature": other["signature"]})


# --------------------------------------------------------------------------
# challenges
# --------------------------------------------------------------------------


def test_a_challenge_redeems_once(redis):
    challenge = service.issue_challenge()
    asyncio.run(service.redeem_challenge(challenge))
    with pytest.raises(AssertionInvalid):
        asyncio.run(service.redeem_challenge(challenge))


def test_an_expired_challenge_is_refused(redis):
    challenge = service.issue_challenge(now=1_000_000)
    with pytest.raises(AssertionInvalid):
        asyncio.run(
            service.redeem_challenge(
                challenge, now=1_000_000 + service.CHALLENGE_TTL_SECONDS + 1
            )
        )


def test_a_challenge_from_a_replica_slightly_ahead_still_redeems(redis):
    challenge = service.issue_challenge(now=1_000_003)
    asyncio.run(service.redeem_challenge(challenge, now=1_000_000))


def test_a_cross_origin_ceremony_is_refused():
    authenticator = Authenticator()
    assertion = authenticator.assert_("abc")
    client_data = json.loads(
        base64.urlsafe_b64decode(assertion["clientDataJSON"] + "==")
    )
    client_data["crossOrigin"] = True
    raw = json.dumps(client_data).encode()
    auth_data = base64.urlsafe_b64decode(assertion["authenticatorData"] + "==")
    signature = authenticator.key.sign(
        auth_data + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256())
    )
    with pytest.raises(AssertionInvalid):
        verify(
            authenticator,
            {
                **assertion,
                "clientDataJSON": b64url(raw),
                "signature": b64url(signature),
            },
        )


def test_a_forged_challenge_is_refused(redis):
    raw = bytearray(base64.urlsafe_b64decode(service.issue_challenge() + "=="))
    raw[0] ^= 0xFF
    with pytest.raises(AssertionInvalid):
        asyncio.run(service.redeem_challenge(b64url(bytes(raw))))


# --------------------------------------------------------------------------
# HTTP contract
# --------------------------------------------------------------------------


def blob_for(pubkey: str, credential_id: str = CREDENTIAL_ID) -> str:
    return json.dumps(
        {
            "v": 1,
            "pubkey": pubkey,
            "credentialId": credential_id,
            "iv": "aa",
            "ciphertext": "bb",
        }
    )


def row_for(pubkey: str, authenticator: Authenticator, **fields):
    return SimpleNamespace(
        credential_id_hash=CREDENTIAL_HASH,
        credential_id=CREDENTIAL_ID,
        pubkey=pubkey,
        credential_public_key=authenticator.spki,
        alg=authenticator.alg,
        blob=blob_for(pubkey),
        label="iCloud Keychain",
        sign_count=0,
        created_at=datetime(2026, 10, 27, 12, 0, 0),
        last_used_at=None,
        **fields,
    )


@pytest.fixture
def api(client, redis):
    db = AsyncMock()

    async def _db():
        yield db

    async def _no_limit():
        return None

    fastapi_app.dependency_overrides[get_db] = _db
    fastapi_app.dependency_overrides[rate_limit_passkey_challenge] = _no_limit
    fastapi_app.dependency_overrides[rate_limit_passkey_open] = _no_limit
    yield client, db


def put_body(authenticator: Authenticator, pubkey: str, **overrides) -> dict:
    return {
        "credentialId": CREDENTIAL_ID,
        "publicKey": authenticator.spki,
        "alg": authenticator.alg,
        "blob": blob_for(pubkey),
        "label": "iCloud Keychain",
        **overrides,
    }


def stored_entry() -> dict:
    """What the upsert's RETURNING hands back."""
    return {
        "credential_id_hash": CREDENTIAL_HASH,
        "credential_id": CREDENTIAL_ID,
        "label": "iCloud Keychain",
        "created_at": datetime(2026, 10, 27, 12, 0, 0),
        "last_used_at": None,
    }


def test_the_owner_stores_a_vault(api, caller, monkeypatch):
    client, _ = api
    authenticator = Authenticator()
    upsert = AsyncMock(return_value=stored_entry())
    monkeypatch.setattr(service, "upsert_passkey_vault_on_db", upsert)

    response = client.put(
        f"/passkeyVault/{CREDENTIAL_HASH}", json=put_body(authenticator, caller.pubkey)
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "credentialIdHash": CREDENTIAL_HASH,
        "credentialId": CREDENTIAL_ID,
        "label": "iCloud Keychain",
        "createdAt": "2026-10-27T12:00:00Z",
        "lastUsedAt": None,
    }
    assert upsert.await_args.kwargs["pubkey"] == caller.pubkey


def test_a_vault_carrying_someone_elses_key_is_refused(api, caller):
    client, _ = api
    body = put_body(Authenticator(), "ab" * 32)
    assert client.put(f"/passkeyVault/{CREDENTIAL_HASH}", json=body).status_code == 422


def test_a_vault_for_another_credential_is_refused(api, caller):
    client, _ = api
    body = put_body(
        Authenticator(), caller.pubkey, blob=blob_for(caller.pubkey, "b3RoZXI")
    )
    assert client.put(f"/passkeyVault/{CREDENTIAL_HASH}", json=body).status_code == 422


def test_a_credential_id_that_does_not_hash_to_the_path_is_refused(api, caller):
    client, _ = api
    body = put_body(Authenticator(), caller.pubkey)
    assert client.put(f"/passkeyVault/{'0' * 64}", json=body).status_code == 422


def test_another_accounts_passkey_row_cannot_be_overwritten(api, caller, monkeypatch):
    client, _ = api
    # The conditional upsert writes nothing when the row is someone else's.
    monkeypatch.setattr(
        service, "upsert_passkey_vault_on_db", AsyncMock(return_value=None)
    )
    body = put_body(Authenticator(), caller.pubkey)
    assert client.put(f"/passkeyVault/{CREDENTIAL_HASH}", json=body).status_code == 403


def test_a_non_canonical_credential_id_is_refused(api, caller):
    client, _ = api
    # Four bytes leave four unused bits in the last character, so "YWJjZB"
    # decodes to the same bytes as the canonical "YWJjZA".
    canonical, sloppy = "YWJjZA", "YWJjZB"
    assert base64.urlsafe_b64decode(sloppy + "==") == base64.urlsafe_b64decode(
        canonical + "=="
    )
    path = hashlib.sha256(b"abcd").hexdigest()
    body = put_body(Authenticator(), caller.pubkey, credentialId=sloppy)
    body["blob"] = blob_for(caller.pubkey, sloppy)
    assert client.put(f"/passkeyVault/{path}", json=body).status_code == 422


def test_an_oversized_blob_is_refused_by_the_schema(api, caller):
    client, _ = api
    body = put_body(Authenticator(), caller.pubkey, blob="x" * 4097)
    assert client.put(f"/passkeyVault/{CREDENTIAL_HASH}", json=body).status_code == 422


def test_a_weak_rsa_key_is_refused(api, caller):
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_keys

    client, _ = api
    weak = rsa_keys.generate_private_key(public_exponent=3, key_size=1024)
    spki = b64url(
        weak.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    body = put_body(Authenticator(), caller.pubkey, publicKey=spki, alg=ALG_RS256)
    assert client.put(f"/passkeyVault/{CREDENTIAL_HASH}", json=body).status_code == 422


def test_the_upsert_only_overwrites_the_owners_row_and_resets_the_counter():
    from sqlalchemy.dialects import postgresql

    from app.repos import passkey_vault_repo

    captured = {}

    class Db:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile(dialect=postgresql.dialect())).replace(
                "\n", " "
            )
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: None)
            )

    asyncio.run(
        passkey_vault_repo.upsert_passkey_vault_on_db(
            Db(),
            credential_id_hash=CREDENTIAL_HASH,
            credential_id=CREDENTIAL_ID,
            pubkey="ab" * 32,
            credential_public_key="k",
            alg=ALG_ES256,
            blob="{}",
            label=None,
        )
    )
    sql = captured["sql"]
    assert "ON CONFLICT (credential_id_hash) DO UPDATE" in sql
    assert "WHERE passkey_vault.pubkey = excluded.pubkey" in sql
    assert "sign_count" in sql.split("DO UPDATE")[1]


def test_a_new_device_opens_the_vault_with_the_passkey(api, caller, monkeypatch):
    client, _ = api
    authenticator = Authenticator()
    row = row_for(caller.pubkey, authenticator)
    monkeypatch.setattr(
        service, "select_passkey_vault_by_hash_on_db", AsyncMock(return_value=row)
    )
    mark = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "mark_passkey_vault_used_on_db", mark)

    challenge = client.post("/passkeyVault/challenge").json()["data"]["challenge"]
    body = {"assertion": authenticator.assert_(challenge)}
    response = client.post(f"/passkeyVault/{CREDENTIAL_HASH}/open", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"blob": row.blob}
    mark.assert_awaited_once()
    # the same assertion again: the challenge is spent
    assert (
        client.post(f"/passkeyVault/{CREDENTIAL_HASH}/open", json=body).status_code
        == 404
    )


@pytest.mark.parametrize(
    "case",
    ["no-row", "wrong-key", "foreign-challenge", "counter-went-back", "counter-raced"],
)
def test_every_failed_open_looks_the_same(api, caller, monkeypatch, case):
    client, _ = api
    authenticator = Authenticator()
    stored = Authenticator() if case == "wrong-key" else authenticator
    row = row_for(caller.pubkey, stored)
    if case == "counter-went-back":
        row.sign_count = 9
    # Another open advanced the counter between our read and our write.
    monkeypatch.setattr(
        service,
        "mark_passkey_vault_used_on_db",
        AsyncMock(return_value=case != "counter-raced"),
    )
    monkeypatch.setattr(
        service,
        "select_passkey_vault_by_hash_on_db",
        AsyncMock(return_value=None if case == "no-row" else row),
    )
    challenge = (
        b64url(b"x" * 56)
        if case == "foreign-challenge"
        else client.post("/passkeyVault/challenge").json()["data"]["challenge"]
    )
    assertion = authenticator.assert_(
        challenge, sign_count=5 if case == "counter-went-back" else 0
    )

    response = client.post(
        f"/passkeyVault/{CREDENTIAL_HASH}/open", json={"assertion": assertion}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


def test_the_owner_lists_their_passkeys(api, caller, monkeypatch):
    client, _ = api
    rows = [row_for(caller.pubkey, Authenticator())]
    select = AsyncMock(return_value=rows)
    monkeypatch.setattr(service, "select_passkey_vaults_by_pubkey_on_db", select)

    response = client.get("/passkeyVault")

    assert response.status_code == 200
    assert [e["credentialIdHash"] for e in response.json()["data"]] == [CREDENTIAL_HASH]
    assert response.json()["data"][0]["credentialId"] == CREDENTIAL_ID
    assert "blob" not in response.json()["data"][0]
    assert select.await_args.args[1] == caller.pubkey


def test_a_delete_is_scoped_to_the_caller(api, caller, monkeypatch):
    client, _ = api
    delete = AsyncMock()
    monkeypatch.setattr(service, "delete_passkey_vault_on_db", delete)

    assert client.delete(f"/passkeyVault/{CREDENTIAL_HASH}").status_code == 200
    delete.assert_awaited_once()
    assert delete.await_args.args[1:] == (CREDENTIAL_HASH, caller.pubkey)


def test_writes_need_a_session():
    from fastapi.testclient import TestClient

    anonymous = TestClient(fastapi_app)
    assert anonymous.get("/passkeyVault").status_code in (401, 403)


@pytest.mark.parametrize(
    "sign_count, condition",
    [
        (0, "passkey_vault.sign_count = %(sign_count_1)s"),
        (7, "passkey_vault.sign_count < %(sign_count_1)s"),
    ],
)
def test_marking_an_open_only_moves_the_counter_forward_on_the_databases_clock(
    sign_count, condition
):
    from sqlalchemy.dialects import postgresql

    from app.repos import passkey_vault_repo

    captured = {}

    class Db:
        async def execute(self, stmt):
            captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
            return SimpleNamespace(rowcount=1)

    assert asyncio.run(
        passkey_vault_repo.mark_passkey_vault_used_on_db(
            Db(), CREDENTIAL_HASH, sign_count
        )
    )
    sql = captured["sql"]
    assert condition in sql
    # created_at is the database's now(); last_used_at must be the same clock.
    assert "last_used_at=now()" in sql
