# ADR 0001: The Assistant's Duty Menu — a per-customer menu of `/setup`'s own rows, built and signed by the server

**Status:** Proposed. Written after the build, on draft PR
[NosFabrica/brainstorm_server#110](https://github.com/NosFabrica/brainstorm_server/pull/110) (commit `58b32b3`). No
Gate A was held. It's for David to accept and for the Gate B reviewer to ratify, together with this ADR's
classification. Revised 2026-10-28 for the PR 110 audit: D4's premise (what the UI copies from `/setup`), D5's read
(now strict) and D6's check.
**Date:** 2026-10-27
**Story:** [`stories/assistant-duty-menu/1-duty-menu.md`](../../stories/assistant-duty-menu/1-duty-menu.md), written
after the build (2026-10-28)
**Issue:** [NosFabrica/brainstorm_server#108](https://github.com/NosFabrica/brainstorm_server/issues/108)

## Context

Brainstorm-UI's fourth Account Setup step (NosFabrica/Brainstorm-UI#292) shows the caller's Brainstorm Assistant's
**Duty Menu**: one kind-39999 event, `d` = `duty-menu`, signed by the Assistant's own key. It says which duties the
Assistant takes on and at which relay each is published. The wire format is Tapestry's pre-NIP,
`protocols/drafts/duty-menus.md` (nous-clawds4/tapestry); the § numbers below are its. The UI's ask is
Brainstorm-UI `docs/assistant/DUTY-MENU-ASK.md`.

Under the Light profile (`../../workflows/light-profile.md` § Gate A), this change trips three irreversibility
triggers:
- a **wire format**: the menu event;
- a **cross-repo contract**: the UI compares the published menu with `GET /user/assistantDutyMenu`;
- a change to `GET /setup/{pubkey}`'s rows (D4), a public answer any client can read as this Assistant's designation
  rows.

So it owes a full ADR. This is that ADR, recording what PR 110 did.

### What Brainstorm adopts from Tapestry

These hold here as Tapestry decided them, and this ADR doesn't reargue them:
- **The menu itself** (ADR `duty-menus-and-self-maps/0002`): one self-signed item per Assistant key, under Nous'
  header `39998:<Nous>:duty-menu` in a `z` tag.
- **One `p`, equal to the author**, and § 6.1's test of what counts as a menu, including its fifth condition: no
  `["z", "list"]`, and no `z` naming a concept header (ADR `duty-menus-and-self-maps/0003` sub-decision 11, carried
  into ADR `assistant-duty-menu/0001` Amendment 2).
- **The key grammar** a row's key is read by (ADR `duty-menus-and-self-maps/0004`).
- **The server builds and signs; the request carries nothing** (ADR `assistant-duty-menu/0002` Option A; Option C,
  the browser building the template, rejected there for the reason it is here).
- **The offer is computed on the server and the UI compares** (ADR `assistant-duty-menu/0001` Option A). There the
  answer rode Tapestry's hub; here it's a route of its own (D7).
- **Dating a new version after the newest found**, and sending it to every relay an earlier version's rows named
  (ADR `assistant-duty-menu/0002` sub-decisions 4 and 5).

### Repo facts this ADR builds on

- One Assistant key per customer, the "brainstorm nsec" (BIBLE § 4). It already signs their Trusted Assertions and
  Trusted Lists, and `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db` reads it.
- `GET /setup/{pubkey}` serves the Assistant's kind-10040 designation rows. It was five `30382:<metric>` rows and
  `30392`. Brainstorm-UI's activation (`signNip85`) doesn't copy them whole: it writes `30382:rank` and
  `30382:followers` at the relay its own config names, and `30392`, `30393` and `30394` from `/setup`'s `30392` row.
- A TA carries the metric tags `ta_signing.ta_tags` writes, `ta_signing.TA_METRIC_TAGS`: `rank`, `followers`,
  `reporters`, `muters`, and `hops` for a reachable Observee.
- The TA relay has a public URL (`nostr_upload_ta_events_relay_public_url`, what consumers are told) and an internal
  one (`nostr_upload_ta_events_relay`, where the server writes).
- The Assistant's NIP-65 list, `assistant_relay_list()`, is built from settings: the TA relay, `trusted_list_relay`,
  then the kind-0 relays.
- nostr-sdk 0.41, checked locally, drops a `p` tag naming the signer unless `allow_self_tagging()` is set. Its
  `fetch_events_from` only logs a relay's `CLOSED`, auth failure or timeout, and returns no events and no error.

## Decisions

### D1 — One menu per customer's Assistant, not a storefront

**Options considered:**

- **A (chosen): a per-customer menu.** Each customer's Assistant signs its own menu, with that Assistant's rows.
  It carries no `storefront` tag (§ 7).
- **B: one storefront** signed by a Brainstorm service key, saying where every customer's Assistant publishes.
  **Rejected:**
  - § 7 lets storefront rows fill entries only for the observer's own Assistant, and never counts them in § 3's
    read-time fallback. So a storefront would not tell a reader where a given customer's Scores are.
  - The UI's step checks the caller's own Assistant's menu (§ 8.1, "One key's menu").
  - A per-key menu is also where per-customer settings would show, if relays ever differ per customer.

**Consequence accepted:** one menu per activated customer, each republished separately when the offer changes.

### D2 — The server owns the offer; the UI compares

- `GET /user/assistantDutyMenu` answers `{assistant, rows}`, the offer as `[key, relay]` rows.
- `POST /user/assistantDutyMenu` takes no body. It builds the menu from the same function, at the same moment's
  settings.
- The UI reads the published menu from relays itself, and calls it current when its `duty` rows, as a set of
  `(key, relay)`, equal the GET's `rows`.

**Options considered:**
- **A (chosen):** as above. The UI compares; it never builds.
- **B: the UI builds the template, the server only signs.** **Rejected**, as in ADR `assistant-duty-menu/0002`:
  the rows would come from the request, and the server would have to re-validate every tag to sign nothing it
  doesn't offer.

### D3 — The rows are `/setup`'s rows, one list, with a key per published metric

`services/duty_menu_build.py` is the one list:
- `designation_rows` serves `/setup`;
- `offer_rows` serves the menu: the same rows as `[key, relay]`, minus any with no relay, since a `duty` row
  without one is invalid (§ 5.1).

The keys are `30382:rank`, `30382:followers`, `30382:reporters`, `30382:muters`, `30382:hops`, then `30392`,
`39998`, `39999`.

**Options considered:**
- **A (chosen): `/setup`'s own rows.** The menu offers exactly the rows `/setup` serves. The two can't drift, because
  they're one function. What a customer's Map gives the Assistant is what their client wrote there, and the Treasure
  Map reads that against the offer by covering (ADR `assistant-treasure-map/0001` D1).
- **B: Tapestry's K1 bare kinds**, a single `30382` row (ADR `assistant-duty-menu/0001`). **Rejected**, by K1's own
  principle, "claims nothing unbuilt":
  - Brainstorm publishes five metrics, and a bare `30382` row would offer every one at the TA relay.
  - It would also make the menu and `/setup` two lists.
- **C: the families** (`3038x`, `3039x`). Rejected for the same reason Tapestry rejected its K2.

**Consequence accepted:** the offer is per key, not per kind. Anything that reads it, the Treasure Map first, reads
it by § 6.2 covering (ADR `assistant-treasure-map/0001` D1).

### D4 — `/setup` and the menu gain `39998` and `39999` at a new `dictionary_relay` setting

`settings.dictionary_relay` defaults to `wss://dcosl.brainstorm.world`.

**Options considered:**
- **A (chosen): add the Concept rows to the one list now.** The Assistant is to manage the customer's Dictionary.
  Offering it lets a Map designate the Assistant for Concepts before its first copy.
- **B: offer only what is published today**, Scores and admin-run Trusted Lists. **Rejected:** the menu and `/setup`
  would both change the day Dictionary management ships, and a client couldn't name the Assistant for Concepts
  before then.
- **C: the menu only, not `/setup`.** **Rejected:** it breaks D3's one list.

**Consequences accepted:**
- **`/setup`'s answer changes:** it lists `39998`/`39999` at `dictionary_relay`, and leaves them out when that
  setting is empty. Brainstorm-UI's activation doesn't copy them into Maps today, so a Map names the Assistant for
  Concepts only where a client writes it so.
- **The offer runs ahead of the build.** Concept publishing is
  [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112), a separate
  follow-up. Until it ships, the menu offers Concepts the Assistant doesn't publish. The Assistant's Treasure Map
  says so: it reports them `not-published-here` and never lists them. The menu says what's offered; the Treasure Map
  says what's active.
- The same already held for `30392`, offered by `/setup` before this change while only `POST /admin/trustedLists`
  publishes Trusted Lists.

### D5 — Where a version is sent: four places, the TA relay at its internal URL

A publish goes, each relay once, to:
- each row's relay;
- the Assistant's outbox, `assistant_relay_list()`;
- every relay a `duty` row of an earlier version named;
- Nous' relay, `NOUS_MENU_RELAYS = ["wss://dcosl.brainstorm.world"]`, fixed rather than read from Nous' kind 10002
  (§ 8.1 item 3).

The TA relay's public URL is written at its internal URL, as scores are (`duty_menu_build.publish_targets`,
`write_url`). The report names public addresses.

**Options considered:**
- **A (chosen):** as above.
- **B: Tapestry's shape** (ADR `assistant-duty-menu/0002` sub-decision 5). Tapestry imports to its own relay first,
  DNS-guards every event-named relay and caps them at 50. **Not adopted:**
  - Brainstorm has no local relay to import into. The TA relay is written over a websocket like any other.
  - The relays named in events come only from earlier versions signed by the Assistant itself. The read checks each
    version's signature (`assistant_publish.is_own`), so they're the server's own past choices, not a stranger's.
- **C: the public URL as given.** **Rejected:** the server writes scores at the internal URL because it can always
  reach it. The menu should land where the scores do.

**The earlier versions are read strictly**, through the REQ-until-EOSE reader the Treasure Map uses
(`assistant_publish.read_newest`), and the publish is a 502 when no relay answers. The first build read them
best-effort, through nostr-sdk's `fetch_events_from`, on ADR `assistant-self-map/0002` R2's ground that a menu
holds nothing else to lose. But that call reads a relay that failed as one holding nothing, so a failed read
dated the new menu now instead of after the newest, which a menu dated ahead would outlast, and skipped the relays
only the old version named.

### D6 — Dated after the newest found, the `p` kept, checked before it is sent

- **The date:** `created_at = max(now, newest version found + 1)` (`menu_created_at`), so a republish always
  replaces the old version, even one dated ahead of us.
- **The `p`:** the event is built with `allow_self_tagging()`, because § 4's one `p` is the Assistant itself.
- **The check:** the signed event must verify, carry exactly the tags built, and pass `is_duty_menu` (§ 6.1) before
  anything is sent (`assistant_publish.sign_checked`, shared with the Treasure Map). If not, the publish is a 500
  with a plain sentence.
  - In the tests, this check caught nostr-sdk dropping the `p` before anything was published.

### D7 — Two routes on the authenticated `/user` router, answered within the UI's 30 seconds

**Options considered:**
- **A (chosen): `GET`/`POST /user/assistantDutyMenu` on `routers/user/router.py`'s authenticated `router`.**
  `routers/router.py` mounts it before `public_router`.
- **B: on `public_router`.** **Rejected:** there, the `/{pubkey}` catch-all answers `GET /user/assistantDutyMenu`
  as a user profile, with a 200. The UI reads any 200 that isn't `{assistant, rows}` as "not on this server yet".
  A test pins the route order.

The POST is synchronous. The earlier-version read, the connect and the send each have a budget (6 s, 4 s, 12 s)
inside the UI's 30 s. It answers `{event, relays: [{url, ok, message}]}`, and 502 when no relay answered the read
or none took the event. A relay that couldn't be reached is reported as `couldn't connect`; the transport's own
error is logged, not answered.

## Consequences

**Enables**
- The UI's fourth setup step: publish the menu, and see it as current or out of date.
- Any client that reads Duty Menus can find this Assistant's menu, by key (§ 8.1) or by Nous' `#z` (§ 8.2), and fill
  Map entries for it (§ 6.2 step 5).
- One list feeds `/setup`, the menu and the Treasure Map's offer.

**Constrains / makes harder**
- The menu's shape is Tapestry's pre-NIP, and Tapestry's readers and Brainstorm-UI's read it the same way. Neither
  repo can change it alone.
- Changing a row now changes three things at once: what `/setup` serves, what the menu offers, and what the
  Treasure Map can list. Existing Maps and published menus stay as they were until republished.

**Debt and follow-ups**
- **Nothing republishes on its own.** When the offer changes (a relay setting, a new row), the UI shows the menu out
  of date and the customer presses publish.
- **No rate limit on the POST**, like `POST /user/assistantProfile`.
- **No DNS guard on event-named relays** (D5). It's safe because every relay a publish connects to comes from
  settings or from a version the Assistant signed. The Treasure Map's first build also sent where the caller's own
  Map pointed; the PR 110 audit found it, and it no longer does (ADR `assistant-treasure-map/0001` D4). Revisit if
  any route ever sends where a stranger's event points.
- **Owed under the Light profile:**
  - this ADR's acceptance;
  - Gate B's ratification of its classification;
  - a review file, Gate B's (`../../workflows/light-profile.md` § Artifacts). The story file was written after the
    build, on 2026-10-28.

## Implementation notes

As built in commit `58b32b3`, with the relay I/O shared with the Treasure Map since `e2ca872` and the PR 110 audit's
fixes after it:
- `app/services/duty_menu_build.py` (pure, no I/O):
  - the rows: `designation_rows`, `offer_rows`, `DICTIONARY_KIND_ROWS`, and `ta_signing.TA_METRIC_TAGS`;
  - the wire shape: `build_menu_tags`, `is_duty_menu`, `DUTY_MENU_HEADER`, `NOUS_MENU_RELAYS`;
  - `menu_created_at`, `earlier_relays`;
  - the targets: `publish_targets`, `write_url`, and `relay_key`, which spells a relay as nostr-sdk reports it.
- `app/services/duty_menu_service.py`: the routes' logic, `read_assistant_duty_menu_offer` and
  `publish_assistant_duty_menu`.
- `app/services/assistant_publish.py`, shared with the Treasure Map: `assistant`, `is_own`, `read_newest`,
  `sign_checked`, `send_to`, `reached_some`.
- `app/routers/user/router.py`: the two routes. `app/routers/setup/router.py`: `/setup` reads `designation_rows`.
- `app/schemas/`: `AssistantDutyMenuOffer`, `AssistantRelayResult`, `PublishedAssistantDutyMenu`, and their two
  response wrappers.
- `app/core/config.py` and `env.example`: `dictionary_relay`.
- Tests: `tests/test_assistant_duty_menu.py` and `tests/test_setup_designation.py`.

## Out of scope

- Storefronts, `not-offered` rows, and a menu's `name`/`json` details (§§ 5.2, 5.4, 7).
- Republishing when the offer changes.
- Publishing Concepts ([NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112)).
- What the Assistant has live, which is ADR `assistant-treasure-map/0001`.
