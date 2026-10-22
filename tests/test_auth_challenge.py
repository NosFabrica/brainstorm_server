"""Login challenge: issued per nonce, answered once, by the pubkey it was issued for."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nostr_sdk import EventBuilder, Keys, Kind, Tag

from app.api import app
from app.core.database import get_db
from app.services import auth_service

LOGIN_KIND = 22242


class _FakeRedis:
    """String keys with TTLs against a hand-driven clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.store: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> str | None:
        entry = self.store.get(key)
        if entry is None or entry[1] <= self.now:
            self.store.pop(key, None)
            return None
        return entry[0]

    async def set(self, key: str, value: str, ex: int) -> bool:
        self.store[key] = (value, self.now + ex)
        return True

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def getdel(self, key: str) -> str | None:
        value = self._live(key)
        self.store.pop(key, None)
        return value

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) else 0


@pytest.fixture
def redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(auth_service, "redis_client", fake)
    return fake


@pytest.fixture
def observer_create(monkeypatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(
        auth_service, "get_or_create_brainstorm_observer_nsec_by_pubkey_on_db", mock
    )
    monkeypatch.setattr(
        auth_service, "current_session_version", AsyncMock(return_value=0)
    )
    return mock


@pytest.fixture
def login(client, redis, observer_create):
    async def _no_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _no_db
    return client


def _challenge(client, pubkey: str) -> str:
    r = client.get(f"/authChallenge/{pubkey}")
    assert r.status_code == 200
    return r.json()["data"]["challenge"]


def _signed(keys: Keys, challenge: str, kind: int = LOGIN_KIND) -> dict:
    event = (
        EventBuilder(Kind(kind), "")
        .tags(
            [
                Tag.parse(["t", "brainstorm_login"]),
                Tag.parse(["challenge", challenge]),
            ]
        )
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def _verify(client, pubkey: str, event: dict):
    return client.post(f"/authChallenge/{pubkey}/verify", json={"signed_event": event})


def test_login_succeeds_while_an_attacker_requests_challenges_for_the_victim(login):
    victim = Keys.generate()
    pubkey = victim.public_key().to_hex()

    challenge = _challenge(login, pubkey)
    for _ in range(5):
        _challenge(login, pubkey)

    r = _verify(login, pubkey, _signed(victim, challenge))

    assert r.status_code == 200
    assert r.json()["data"]["token"]


def test_a_consumed_login_event_cannot_be_replayed(login):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()
    event = _signed(user, _challenge(login, pubkey))

    assert _verify(login, pubkey, event).status_code == 200
    assert _verify(login, pubkey, event).status_code == 401


def test_a_signature_of_another_kind_is_not_a_login(login):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()

    r = _verify(login, pubkey, _signed(user, _challenge(login, pubkey), kind=1))

    assert r.status_code == 401


def test_an_expired_challenge_is_rejected(login, redis):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()
    challenge = _challenge(login, pubkey)

    redis.now += 5 * 60

    assert _verify(login, pubkey, _signed(user, challenge)).status_code == 401


def test_a_challenge_lives_long_enough_for_a_slow_signer(login, redis):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()
    challenge = _challenge(login, pubkey)

    redis.now += 5 * 60 - 1

    assert _verify(login, pubkey, _signed(user, challenge)).status_code == 200


def test_a_challenge_issued_for_another_pubkey_is_rejected(login):
    attacker = Keys.generate()
    victim_pubkey = Keys.generate().public_key().to_hex()
    attacker_pubkey = attacker.public_key().to_hex()
    challenge = _challenge(login, victim_pubkey)

    r = _verify(login, attacker_pubkey, _signed(attacker, challenge))

    assert r.status_code == 401


def test_an_event_by_someone_other_than_the_path_pubkey_is_rejected(login):
    victim_pubkey = Keys.generate().public_key().to_hex()
    attacker = Keys.generate()
    challenge = _challenge(login, victim_pubkey)

    r = _verify(login, victim_pubkey, _signed(attacker, challenge))

    assert r.status_code == 401


def test_a_tampered_event_is_rejected(login):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()
    event = _signed(user, _challenge(login, pubkey))
    event["content"] = "tampered"

    assert _verify(login, pubkey, event).status_code == 401


def test_login_creates_the_observer(login, observer_create):
    user = Keys.generate()
    pubkey = user.public_key().to_hex()

    _verify(login, pubkey, _signed(user, _challenge(login, pubkey)))

    assert observer_create.await_args.args[1] == pubkey


_UNDER_O = """
import sys
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from nostr_sdk import Keys
import tests.conftest
from tests.test_auth_challenge import _FakeRedis, _challenge, _signed, _verify
from app.api import app
from app.core.database import get_db
from app.services import auth_service
from app.utils.rate_limiting import rate_limiting

assert False, "asserts must be stripped"
auth_service.redis_client = _FakeRedis()
rate_limiting.get_redis_client = lambda: tests.conftest._LimiterRedis()
auth_service.get_or_create_brainstorm_observer_nsec_by_pubkey_on_db = AsyncMock()
async def _no_db():
    yield AsyncMock()
app.dependency_overrides[get_db] = _no_db
client = TestClient(app)
user, other = Keys.generate(), Keys.generate()
pubkey = user.public_key().to_hex()
event = _signed(other, _challenge(client, pubkey))
event["pubkey"] = pubkey
cases = {
    "wrong author": _signed(other, _challenge(client, pubkey)),
    "wrong kind": _signed(user, _challenge(client, pubkey), kind=1),
    "unknown challenge": _signed(user, "f" * 32),
    "forged signature": event,
}
failed = [n for n, e in cases.items() if _verify(client, pubkey, e).status_code != 401]
sys.exit(f"accepted under -O: {failed}" if failed else 0)
"""


def test_checks_still_run_under_python_O():
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "-O", "-c", _UNDER_O],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
