"""The periodic half of billing: taking back what lapsed, and asking Flash about
what we cannot judge locally.

Split from `billing_service` because the two change for different reasons — that
one moves when Flash's webhook shape does, this one when the cron's cadence or
batching does. Both decisions still come from `billing_service`, so neither the
status rule nor the admin-override rule can drift between the event path and
these.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.flash import UNKNOWN_LIFECYCLE_POLICY, FlashCredentialError, FlashUnavailable
from app.core.loggr import loggr
from app.repos.flash_webhook_event_repo import (
    claim_webhook_event_on_db,
    mark_webhook_event_processed_on_db,
    record_webhook_event_failure_on_db,
    select_abandoned_webhook_events_on_db,
)
from app.repos.user_subscription_repo import (
    AbandonRule,
    clear_granted_scheduling_on_db,
    record_sync_failure_on_db,
    select_entitlement_candidates_on_db,
    select_reconcile_candidates_on_db,
    update_last_event_at_on_db,
)
from app.repos.brainstorm_nsec import (
    get_scheduling_source_on_db,
    set_scheduling_for_pubkey_on_db,
)
from app.services.flash_webhook_service import delivery_target
from app.services.billing_service import (
    SETTLED_REASONS,
    EntitlementDecision,
    EntitlementReason,
    is_admin_held,
    utc_now,
    apply_entitlement,
    apply_payload_fallback,
    decide_entitlement,
    resolve_entitlement,
)

logger = loggr.get_logger(__name__)


async def revoke_lapsed_entitlements(
    db: AsyncDBSession, *, now: datetime | None = None
) -> int:
    """Take back policies whose paid period ran out. Returns how many.

    `decide_entitlement` and `resolve_entitlement` both judge, exactly as they do
    on the event path — so neither the status rule nor the admin-override rule
    can drift between the two. Flash does send `subscription.expired`,
    so this is the backstop for the delivery that never arrives: Flash stops
    retrying after a few attempts and never replays.

    Note what this deliberately does NOT catch. A `past_due` row, or one still
    recorded `active` whose period end has passed, both HOLD — locally we cannot
    tell "renewal failed" from "renewal succeeded and we missed the event", and
    revoking on that ambiguity would cut off someone who is paying. Those need
    Flash re-read, which is the reconcile path's job, not a sweep's.
    """
    # Normalized here rather than trusted from the caller: prod stores some of
    # these columns as timestamptz where staging and local are naive, so a
    # caller-supplied clock in the wrong epoch would compare silently wrong.
    at = now or utc_now()
    revoked = 0
    for row in await select_entitlement_candidates_on_db(db):
        if row.granted_scheduling_id is None:
            continue  # the query excludes these; belt and braces
        decision = decide_entitlement(
            row.flash_status,
            cancel_effective_date=row.cancel_effective_date,
            current_period_end=row.current_period_end,
            now=at,
            # The sweep reads the stored row, which records what Flash said
            # about the subscription and nothing about the policy behind it.
            # Unknown is the honest input, and unknown never revokes — so a
            # subscriber mid-dunning is only ever revoked after asking Flash.
            policy=UNKNOWN_LIFECYCLE_POLICY,
        )
        if decision is not EntitlementDecision.REVOKE:
            continue
        resolution = resolve_entitlement(
            decision,
            blocked=False,
            admin_held=is_admin_held(
                await get_scheduling_source_on_db(db, row.pubkey)
            ),
            plan_scheduling_id=row.granted_scheduling_id,
            existing_granted=row.granted_scheduling_id,
        )
        if not resolution.write_policy or resolution.source is None:
            # An admin put them there; their period lapsing is not ours to act on.
            continue

        await set_scheduling_for_pubkey_on_db(
            db, row.pubkey, resolution.scheduling_id, source=resolution.source
        )
        await clear_granted_scheduling_on_db(db, row.pubkey)
        # Committed per row: the sweep is idempotent, so partial progress
        # surviving a crash beats an all-or-nothing batch losing every revocation
        # it had already made.
        await db.commit()
        revoked += 1
        logger.info("%s lost their paid policy: the paid period ran out", row.pubkey)

    return revoked


# Outcomes where Flash answered but nothing was written — so nothing recorded
# that we asked, and the candidate ordering would keep re-asking about them.
_UNSETTLED_REASONS = frozenset(
    {
        EntitlementReason.UNKNOWN_USER,
        EntitlementReason.UNKNOWN_PLAN,
        EntitlementReason.UNKNOWN_SUBSCRIPTION,
        EntitlementReason.REFERENCE_MISMATCH,
        EntitlementReason.NO_REFERENCE,
    }
)


@dataclass(frozen=True)
class ReconcileResult:
    reconciled: int
    failed: int


async def reconcile_subscriptions(
    db: AsyncDBSession,
    *,
    limit: int,
    stale_after: timedelta,
    abandon_pending_after: timedelta,
    now: datetime | None = None,
) -> ReconcileResult:
    """Re-read Flash for subscribers no event has settled.

    Flash gives up on an undelivered webhook after a few retries and never
    replays it, so this is the only path that recovers one. It is also the only
    thing that can resolve a `past_due` row or one still recorded `active` past
    its period end — locally those are indistinguishable from "renewal succeeded
    and we missed the event", which is why the lapse sweep refuses to judge them.
    """
    at = now or utc_now()
    candidates = await select_reconcile_candidates_on_db(
        db,
        now=at,
        stale_after=stale_after,
        limit=limit,
        abandoned=AbandonRule(
            after=abandon_pending_after,
            error=EntitlementReason.UNKNOWN_SUBSCRIPTION.value,
        ),
    )

    reconciled = failed = 0
    for row in candidates:
        try:
            outcome = await apply_entitlement(
                db, external_ref=row.pubkey, subscription_id=None
            )
            if outcome.reason in _UNSETTLED_REASONS:
                # Flash answered, but not in a way that settles anything: no
                # subscription, an unmapped plan, a reference that disagrees.
                # These never reach the upsert, so nothing advanced their read
                # clock — and the candidate query orders by it, so leaving them
                # would park them at the head of a bounded batch forever.
                failed += 1
                await record_sync_failure_on_db(db, row.pubkey, outcome.reason.value)
                await db.commit()
                logger.warning(
                    "Reconciling %s settled nothing (%s)", row.pubkey, outcome.reason.value
                )
                continue
            reconciled += 1
        except FlashCredentialError:
            # Every remaining row would fail identically. Continuing would bury
            # the one thing a human needs to see under a copy of it per subscriber.
            await db.rollback()
            failed += 1
            await record_sync_failure_on_db(db, row.pubkey, "credentials refused")
            await db.commit()
            logger.error(
                "Flash refused our credentials; abandoning this reconcile batch"
            )
            break
        except FlashUnavailable as unavailable:
            await db.rollback()
            failed += 1
            await record_sync_failure_on_db(db, row.pubkey, str(unavailable))
            await db.commit()
            logger.warning(
                "Could not reconcile %s (%s); their tier is untouched",
                row.pubkey,
                unavailable,
            )

    return ReconcileResult(reconciled=reconciled, failed=failed)


async def replay_unprocessed_events(
    db: AsyncDBSession, *, limit: int, stale_after: timedelta, max_attempts: int
) -> int:
    """Finish events we acknowledged and then dropped. Returns how many.

    Flash retries an undelivered webhook a few times and then never replays it,
    so once we answered 200 the event is ours to not lose. A process killed
    between the acknowledgement and the entitlement write leaves exactly this:
    a recorded event nothing ever acted on.

    Claiming is decided by the database, not by whoever read first, so this is
    exactly-once even with several replicas running.
    """
    at = utc_now()
    replayed = 0

    for event in await select_abandoned_webhook_events_on_db(
        db, now=at, stale_after=stale_after, max_attempts=max_attempts, limit=limit
    ):
        claimed = await claim_webhook_event_on_db(
            db, event.id, now=at, stale_after=stale_after
        )
        await db.commit()
        if not claimed:
            # Someone else has it. Losing that race means the work is being
            # done, not that it needs doing twice.
            continue

        if event.payload is None:
            # Nothing writes a null payload, so this means the row was edited by
            # hand. Marking it done would silently discard the work; leaving it
            # is what a human sees in the divergence report.
            logger.error("Flash event %s has no payload to apply", event.id)
            await record_webhook_event_failure_on_db(db, event.id, "payload missing")
            await db.commit()
            continue

        target = delivery_target(event.payload)
        try:
            outcome = await apply_entitlement(
                db,
                external_ref=target.external_ref,
                subscription_id=target.subscription_id,
                yield_if_busy=True,
            )
            if outcome.reason not in SETTLED_REASONS:
                # Nothing was decided — a live delivery holds this subscriber, or
                # the event names nobody we know. Marking it done would discard
                # the work; leaving it brings it back once the claim goes stale.
                await record_webhook_event_failure_on_db(
                    db, event.id, outcome.reason.value
                )
                await db.commit()
                continue
            await mark_webhook_event_processed_on_db(db, event.id, now=at)
            await update_last_event_at_on_db(
                db, pubkey=target.external_ref, event_timestamp=event.event_timestamp
            )
            await db.commit()
            replayed += 1
        except FlashUnavailable as unavailable:
            # Same fallback as the live path: apply what the payload
            # unambiguously implies, leave the event to come back round.
            await db.rollback()
            await record_webhook_event_failure_on_db(db, event.id, str(unavailable))
            await db.commit()
            await apply_payload_fallback(
                db, event=event.event, external_ref=target.external_ref
            )
        except Exception as failed:
            # Rolled back first: if the failure was itself a database error the
            # session is already aborted, and recording it would fail too —
            # taking the rest of the batch down with it.
            await db.rollback()
            # Left unprocessed on purpose: once the claim goes stale it comes
            # back round, up to the attempt cap.
            await record_webhook_event_failure_on_db(db, event.id, repr(failed))
            await db.commit()
            logger.exception("Replaying Flash event %s failed", event.id)

    return replayed
