# app/models

Internal-only Pydantic data classes. **Not** API request / response shapes — those live in [../schemas/](../schemas/) and have their own [CLAUDE.md](../schemas/CLAUDE.md).

If a class is exchanged over HTTP, it goes in `app/schemas/`. If it's purely an in-process value object (queue message, intermediate result), it goes here.

## Current contents

### `grapeRankResult.py`

The GrapeRank algorithm's output, used both internally and as the queue-message payload picked up by the Nostr-publishing consumer.

- **`ScoreCard`** — per-(observer, observee) result. Fields: `observer`, `observee`, `context` (default `"not a bot"`), `average_score`, `input`, `confidence`, `influence`, `verified`, `hops`, `trusted_followers`, `trusted_reporters`.
- **`GrapeRankResult`** — top-level container. Holds:
  - `scorecards: dict[str, ScoreCard] | None` — keyed by observee pubkey.
  - `rounds: int | None`, `duration_seconds: float`, `success: bool` — run telemetry.
  - `changedScorePubkeys: list[str]` — incremental-publish hint (only these need new TA events).
  - `droppedBelowCutoffPubkeys: list[str]` — must be deleted from TA + Vespa.
  - `error: GrapeRankError | None` — structured failure (imported from [`app/schemas/schemas.py`](../schemas/schemas.py); the only schemas → models import).

Consumed in [../message_queue_tasks/upload_nostr_events.py](../message_queue_tasks/upload_nostr_events.py). When changing fields, also touch:

1. The GrapeRank worker that produces this object (out of repo — talk to the algorithm side).
2. `upload_nostr_events.py` — the consumer.
3. [`upsert_scores_to_vespa`](../message_queue_tasks/upload_nostr_events.py) for the Vespa-mirror path.

## Should I add a file here?

Yes, if the type is an internal-only value object passed between modules (queues, service boundaries, helpers).

No, if it's part of an HTTP contract — put it in [`app/schemas/`](../schemas/) and follow the wrapper convention.

Naming: `camelCase.py` for filenames stays consistent with `grapeRankResult.py`. Class names: `PascalCase`.
