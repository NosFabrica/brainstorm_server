"""Request/response shapes for the admin Trusted List trigger."""
from pydantic import BaseModel


class TrustedListTagResult(BaseModel):
    slug: str
    d_tag: str
    tag_event_id: str
    status: str
    taggings_considered: int
    member_count: int
    error: str | None = None
    # 30392 (people), 30393 (events by id) or 30394 (by coordinate).
    kind: int = 30392
    # A content list's tagging target: an event kind, or a DList's header.
    tagging_target: str | None = None


class TrustedListRunData(BaseModel):
    """One admin-triggered run for one Observer.

    The counts are not decoration — they are the story's AC15 visibility
    mitigation. `taggings_in_store == 0` means nothing was ever ingested (the
    un-synced-relay case), which is a different problem from a populated store
    where nobody cleared the rank threshold, and `empty_reason` names which.
    """

    observer: str
    signing_pubkey: str | None = None
    taggings_in_store: int
    qualifying_asserters: int
    dictionary_size: int
    published: int
    failed: int
    retracted: int
    empty_reason: str | None = None
    tags: list[TrustedListTagResult]
    # Content lists (ADR trusted-lists/0002 D8): event taggings held, targets
    # no lookup found this run, tagged kinds with no name (so no list), and why
    # no content list was published or retracted, when none was.
    event_taggings_in_store: int = 0
    unresolved_targets: int = 0
    unnamed_kinds: list[int] = []
    content_skipped_reason: str | None = None
