from fastapi import APIRouter

from app.core.loggr import loggr
from app.schemas.request_response_schemas import TrustedListRunResponse
from app.schemas.trusted_list_schemas import TrustedListRunData, TrustedListTagResult
from app.services.trusted_list_service import generate_trusted_lists_for_observer
from app.utils.nostr import resolve_pubkey_or_400

logger = loggr.get_logger(__name__)

router = APIRouter()


@router.post(
    path="/{observer_pubkey}",
    summary="Admin: generate and publish Trusted Lists for one Observer",
    description=(
        "Computes the Observer's dictionary — the Tags used by asserters whose "
        "Rank in that Observer's web of trust clears the threshold — and "
        "publishes one Trusted List per entry, signed by the Observer's "
        "assistant key: kind 30392 for the people tagged with a Tag, and one "
        "kind 30393 (events by id) or 30394 (by coordinate) per Tag and "
        "tagging target (an event kind, or a DList) for tagged content. The "
        "Observer is the customer the lists are computed FOR; the caller is "
        "the admin triggering it."
    ),
)
async def generate_trusted_lists_endpoint(
    observer_pubkey: str,
) -> TrustedListRunResponse:
    observer_hex = resolve_pubkey_or_400(observer_pubkey, "observer_pubkey")

    run = await generate_trusted_lists_for_observer(observer_hex)
    return TrustedListRunResponse(
        data=TrustedListRunData(
            observer=run.observer,
            signing_pubkey=run.signing_pubkey,
            taggings_in_store=run.taggings_in_store,
            qualifying_asserters=run.qualifying_asserters,
            dictionary_size=run.dictionary_size,
            published=run.published,
            failed=run.failed,
            retracted=run.retracted,
            empty_reason=run.empty_reason,
            event_taggings_in_store=run.event_taggings_in_store,
            unresolved_targets=run.unresolved_targets,
            unnamed_kinds=run.unnamed_kinds,
            content_skipped_reason=run.content_skipped_reason,
            tags=[
                TrustedListTagResult(
                    slug=t.slug,
                    d_tag=t.d_tag,
                    tag_event_id=t.tag_event_id,
                    status=t.status,
                    taggings_considered=t.taggings_considered,
                    member_count=t.member_count,
                    error=t.error,
                    kind=t.kind,
                    tagging_target=t.tagging_target,
                )
                for t in run.tags
            ],
        )
    )
