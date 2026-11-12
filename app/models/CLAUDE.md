# app/models

Internal-only data types (Pydantic classes or `TypedDict`s). **Not** API request / response shapes — those live in [../schemas/](../schemas/) and have their own [CLAUDE.md](../schemas/CLAUDE.md).

If a class is exchanged over HTTP, it goes in `app/schemas/`. If it's purely an in-process value object (queue message, intermediate result), it goes here.

## Current contents

### `grapeRankResult.py`

The GrapeRank algorithm's output, used both internally and as the queue-message payload picked up by the three result consumers.

**`TypedDict`s, not Pydantic models**: the payload is parsed with `orjson` straight into dicts (about 5× faster than `json.loads` + `model_validate` on a 120 MB result) — we only consume what our own worker produces. So no defaults and no coercion: read optional fields with `.get`, and keep the `int()` on `hops` / `trusted_*` at use sites while a pre-cutover worker may still send them as floats.

- **`ScoreCard`** — per-(observer, observee) result. Keys: `observer`, `observee`, `context`, `averageScore`, `input`, `confidence`, `influence`, `verified`, `hops`, `trusted_followers`, `trusted_reporters`, `trusted_muters` (absent from older workers). Consumers read only `observer`, `observee`, `influence`, `hops` and the `trusted_*` counts. The three `trusted_*` counts and `hops` ride this object into the Trusted Assertion's tags; of the counts, only `trusted_followers` is persisted to Neo4j (with `influence` and `hops`).
- **`GrapeRankResult`** — top-level container. Holds:
  - `scorecards: dict[str, ScoreCard] | None` — keyed by observee pubkey.
  - `rounds: int | None`, `duration_seconds: float`, `success: bool` — run telemetry.
  - `changedScorePubkeys: list[str]` — incremental-publish hint (only these need new TA events). Deletes are computed from the `fell_off` diff, not an algo hint.
  - `neo4jWritePubkeys: list[str] | None` — scorecards whose Neo4j-persisted fields (rounded `influence`, `hops`, `trusted_followers`) differ from what Neo4j held when the run started. None/absent (older worker) = write all.
  - `error: dict | None` — structured failure; validated into `GrapeRankError` ([`app/schemas/schemas.py`](../schemas/schemas.py)) only when written to the run row.

Consumed in [../message_queue_tasks/upload_nostr_events.py](../message_queue_tasks/upload_nostr_events.py). When changing fields, also touch:

1. The GrapeRank worker that produces this object (out of repo — talk to the algorithm side).
2. `upload_nostr_events.py` — the consumer.

## Should I add a file here?

Yes, if the type is an internal-only value object passed between modules (queues, service boundaries, helpers).

No, if it's part of an HTTP contract — put it in [`app/schemas/`](../schemas/) and follow the wrapper convention.

Naming: `camelCase.py` for filenames stays consistent with `grapeRankResult.py`. Class names: `PascalCase`.
