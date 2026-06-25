"""Fast, service-mocked tests for ``POST /user/followList``."""

import pytest
from fastapi import HTTPException
from neo4j.exceptions import TransientError
from nostr_sdk import Keys

from tests.conftest import signed_event, signed_kind3


def test_valid_follow_list_returns_200_with_follow_count(
    client, caller, mock_kind3_write
):
    follows = [Keys.generate().public_key().to_hex() for _ in range(3)]
    body = {"signed_event": signed_kind3(caller.keys, follows)}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 200
    assert response.json()["data"]["followCount"] == 3
    # The reused kind-3 handler is invoked once with the original signed event.
    assert mock_kind3_write.await_count == 1
    _session, passed_event = mock_kind3_write.await_args.args
    assert passed_event == body["signed_event"]


def test_empty_follow_list_is_accepted_with_zero_count(
    client, caller, mock_kind3_write
):
    body = {"signed_event": signed_kind3(caller.keys, [])}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 200
    assert response.json()["data"]["followCount"] == 0
    assert mock_kind3_write.await_count == 1


def test_wrong_kind_is_rejected_400(client, caller, mock_kind3_write):
    # A kind-1 event signed by the caller is not a follow list.
    body = {"signed_event": signed_event(caller.keys, kind=1)}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 400
    assert mock_kind3_write.await_count == 0


def test_bad_signature_is_rejected_401(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [Keys.generate().public_key().to_hex()])
    sig = event["sig"]
    event["sig"] = ("1" if sig[0] == "0" else "0") + sig[1:]  # tamper one hex char
    body = {"signed_event": event}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 401
    assert mock_kind3_write.await_count == 0


def test_author_mismatch_is_rejected_403(client, caller, mock_kind3_write):
    # Validly signed by someone other than the authenticated caller.
    other = Keys.generate()
    body = {
        "signed_event": signed_kind3(other, [Keys.generate().public_key().to_hex()])
    }

    response = client.post("/user/followList", json=body)

    assert response.status_code == 403
    assert mock_kind3_write.await_count == 0


def test_rate_limit_exceeded_returns_429(
    client, caller, mock_kind3_write, mock_rate_limit
):
    mock_rate_limit.side_effect = HTTPException(
        status_code=429, detail="Too many requests"
    )
    body = {"signed_event": signed_kind3(caller.keys, [])}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 429
    assert mock_kind3_write.await_count == 0  # rejected before any write


def test_transient_neo4j_error_is_retried_then_succeeds(
    client, caller, mock_kind3_write
):
    mock_kind3_write.side_effect = [TransientError("deadlock"), None]
    body = {
        "signed_event": signed_kind3(
            caller.keys, [Keys.generate().public_key().to_hex()]
        )
    }

    response = client.post("/user/followList", json=body)

    assert response.status_code == 200
    assert response.json()["data"]["followCount"] == 1
    assert mock_kind3_write.await_count == 2  # one retry


def test_persistent_transient_error_surfaces_and_does_not_return_200(
    client, caller, mock_kind3_write
):
    mock_kind3_write.side_effect = TransientError("deadlock")
    body = {"signed_event": signed_kind3(caller.keys, [])}

    with pytest.raises(TransientError):
        client.post("/user/followList", json=body)

    assert mock_kind3_write.await_count == 3  # bounded attempts, then gives up


# --- Structural validation (NIP-01 envelope) -> 422, never a 500 ---------------


def test_missing_sig_is_rejected_422(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [])
    del event["sig"]

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_bad_hex_pubkey_is_rejected_422(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [])
    event["pubkey"] = "zz"  # not 64-hex

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_non_int_created_at_is_rejected_422(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [])
    event["created_at"] = "not-a-timestamp"

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_empty_tag_is_rejected_422(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [])
    event["tags"] = [[]]

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_p_tag_without_pubkey_is_rejected_422(client, caller, mock_kind3_write):
    # ["p"] passes nostr_sdk but would IndexError in the shared kind-3 handler.
    event = signed_kind3(caller.keys, [])
    event["tags"] = [["p"]]

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_p_tag_with_bad_hex_follow_is_rejected_422(client, caller, mock_kind3_write):
    event = signed_kind3(caller.keys, [])
    event["tags"] = [["p", "not-a-pubkey"]]

    response = client.post("/user/followList", json={"signed_event": event})

    assert response.status_code == 422
    assert mock_kind3_write.await_count == 0


def test_openapi_documents_event_shape_and_error_contract(client):
    schema = client.app.openapi()

    # The signed event is documented as a NostrEvent with its NIP-01 fields,
    # not an opaque object.
    nostr_event = schema["components"]["schemas"]["NostrEvent"]
    assert set(nostr_event["properties"]) >= {
        "id",
        "pubkey",
        "created_at",
        "kind",
        "tags",
        "content",
        "sig",
    }

    # The route advertises its error contract so consumers see it in Swagger.
    responses = schema["paths"]["/user/followList"]["post"]["responses"]
    assert {"400", "401", "403", "422", "429"} <= set(responses)


def test_event_that_passes_schema_but_fails_parse_is_400_not_500(
    client, caller, mock_kind3_write, monkeypatch
):
    # Backstop: anything nostr_sdk rejects after Pydantic accepts it (e.g. a
    # canonical-serialization quirk) must be a clean 400, never an unhandled 500.
    from nostr_sdk import NostrSdkError

    def _boom(_json: str):
        raise NostrSdkError.Generic("nope")

    monkeypatch.setattr(
        "app.services.onboarding_service.Event.from_json", _boom
    )
    body = {"signed_event": signed_kind3(caller.keys, [])}

    response = client.post("/user/followList", json=body)

    assert response.status_code == 400
    assert mock_kind3_write.await_count == 0
