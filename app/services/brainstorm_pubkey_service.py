from app.db_models import BrainstormNsec
from app.schemas.schemas import BrainstormPubkeyInstance, BrainstormRequestInstance
from nostr_sdk import Keys


def brainstorm_pubkey_db_obj_to_schema_converter(
    brainstorm_nsec_db_obj: BrainstormNsec,
    triggered_graperank: BrainstormRequestInstance | None,
) -> BrainstormPubkeyInstance:
    brainstorm_pubkey_obj = BrainstormPubkeyInstance(
        global_pubkey=brainstorm_nsec_db_obj.pubkey,
        brainstorm_pubkey=Keys.parse(secret_key=brainstorm_nsec_db_obj.nsec)
        .public_key()
        .to_hex(),
        triggered_graperank=triggered_graperank,
        created_at=brainstorm_nsec_db_obj.created_at,
        updated_at=brainstorm_nsec_db_obj.updated_at,
    )

    return brainstorm_pubkey_obj
