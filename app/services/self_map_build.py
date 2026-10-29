"""Pure grammar for Your Assistant's Treasure Map: what the Assistant's own kind 10040 lists.

No I/O. The Map is the Assistant's own kind 10040, naming itself for the duties
it is publishing now: a *self-Map* (Tapestry's `protocols/drafts/treasure-maps.md`
§ 9.2, nous-clawds4/tapestry; the § numbers below are its). Each self-entry
`[key, A, relay]` says that A publishes at that relay, and keeps up to date, the
insights the key wins. The Duty Menu says what the Assistant offers
(`duty_menu_build`); this says what is active.

Ported from Tapestry's `src/lib/self-map/index.js` (ADRs assistant-self-map/
0001–0003), with two changes for Brainstorm:

- **The offer is per key.** `duty_menu_build.offer_rows` offers `30382:rank` …
  `30382:hops`, `30392`, `39998` and `39999`, so it is read by Duty Menus § 6.2's
  covering: a probe's relay is its most specific matching row's, and a key's is
  the most specific row covering it. Tapestry reads bare-kind rows only, which
  would leave Scores not offered here. A key no row covers, such as a bare
  `30382`, is narrowed to the rows within it.
- **Liveness is Brainstorm's own record** (`ScoresUpkeep`, which
  `self_map_service` gathers). Trusted Lists are never live, since only
  `POST /admin/trustedLists` publishes them, and the Assistant publishes no
  Concepts yet.

The engine reasons over a finite set of *probe* insights, each standing for one
region of insights that every key either matches whole or not at all (ADR 0001
sub-decision 1). It knows kind slots (an exact kind, `3038x`, `3039x`, `*`),
Score metric keys (`<kind>:<metric>`) and Concept keys (`39998`, `39999`,
`39998:<d>`, `39999:<d>`). Any other key shape is *unchecked*: never claimed,
and as a duty it keeps a broader self-entry from winning it.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TypeVar

from app.message_queue_tasks.ta_signing import TA_METRIC_TAGS
from app.services.duty_menu_build import relay_key

SELF_MAP_KIND = 10040

HEX64 = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
# The kind slots the engine reads (§ 4.2): an exact kind, a family, or everything.
# ASCII digits only: `\d` takes any Unicode digit, and `int()` reads them, so a
# fullwidth `３０３８２` would pass for 30382 and be signed as written.
KIND_SLOT = re.compile(r"[0-9]{5}|3038x|3039x|\*")
RELAY_URL = re.compile(r"wss?://\S+", re.IGNORECASE)

# The metrics our TAs carry (`ta_signing.TA_METRIC_TAGS`; `hops` only for a
# reachable Observee), read as the observer's supported Scores here, as Tapestry
# reads its own published metrics.
PUBLISHED_SCORE_METRICS = TA_METRIC_TAGS

SCORE_KIND = 30382
SCORE_KINDS = tuple(range(30380, 30390))
LIST_KINDS = tuple(range(30390, 30400))
CONCEPT_KINDS = (39998, 39999)
SYSTEM_WORDS = ("tag", "pin", "dlist", "contexts")

# Spellings that name nothing (§§ 4.5–4.6): readers ignore an entry keyed so.
NAMELESS_KEYS = frozenset({"39998:", "39999:", "39999:dlist-header", "39999:duty-menu"})


def is_score_kind(kind: int) -> bool:
    return 30380 <= kind <= 30389


def is_list_kind(kind: int) -> bool:
    return 30390 <= kind <= 30399


def is_concept_kind(kind: int) -> bool:
    return kind in CONCEPT_KINDS


# --- Keys ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What part of its kinds a key or probe stands for: `all`, one `metric`,
    one Concept `d`, an `unchecked` key's own region (`value` is that key), or,
    for a probe only, `other`: the insights no key names."""

    type: str
    value: str = ""


@dataclass(frozen=True)
class Key:
    norm: str
    slot: str
    kinds: tuple[int, ...]
    segs: tuple[str, ...]
    # § 6's order of kind slots: higher is more specific.
    rank: int
    scope: Scope


@dataclass(frozen=True)
class Probe:
    kind: int
    scope: Scope


def normalize_key(key: str) -> str:
    """A key in its one spelling (§§ 4.1, 4.4, 4.5): trailing empty segments
    dropped, except in a Concept key, which keeps its `d` verbatim; and
    `39998:dlist-header` is the bare `39998`."""
    colon = key.find(":")
    slot = key if colon < 0 else key[:colon]
    rest = "" if colon < 0 else key[colon + 1 :]
    if slot in ("39998", "39999"):
        return "39998" if slot == "39998" and rest == "dlist-header" else key
    segments = rest.split(":") if rest else []
    while segments and segments[-1] == "":
        segments.pop()
    return ":".join([slot, *segments])


def _concept_reading(slot: str) -> tuple[tuple[int, ...], int]:
    """The Concept kinds a bare Concept slot covers, and its rank (ADR 0001
    sub-decision 4): a bare `39998` covers every Concept key, `39999` headers
    included; a bare `39999` the `39999:<d>` keys; a `39999` slot beats a bare
    `39998`."""
    return (CONCEPT_KINDS, 2) if slot == "39998" else ((39999,), 3)


def parse_key(key: Any) -> Key | None:
    """A key, parsed for the engine; None when its kind slot doesn't parse, and
    for the spellings that name nothing (NAMELESS_KEYS)."""
    if not isinstance(key, str):
        return None
    colon = key.find(":")
    slot = key if colon < 0 else key[:colon]
    if not KIND_SLOT.fullmatch(slot):
        return None
    norm = normalize_key(key)
    if norm in NAMELESS_KEYS:
        return None
    if slot in ("39998", "39999"):
        kinds, rank = _concept_reading(slot)
        if norm == slot:
            return Key(norm, slot, kinds, (), rank, Scope("all"))
        d = norm[len(slot) + 1 :]
        return Key(norm, slot, (int(slot),), (d,), 3, Scope("d", d))
    segs = tuple(norm.split(":")[1:])
    if slot == "*":
        kinds = SCORE_KINDS + LIST_KINDS + (() if segs else CONCEPT_KINDS)
        rank = 0
    elif slot == "3038x":
        kinds, rank = SCORE_KINDS, 1
    elif slot == "3039x":
        kinds, rank = LIST_KINDS, 1
    else:
        kinds, rank = (int(slot),), 3
    if not segs:
        scope = Scope("all")
    elif (
        slot != "*"
        and all(is_score_kind(kind) for kind in kinds)
        and len(segs) == 1
        and segs[0] != ""
        and segs[0] not in SYSTEM_WORDS
    ):
        scope = Scope("metric", segs[0])
    else:
        scope = Scope("unchecked", norm)
    return Key(norm, slot, kinds, segs, rank, scope)


def compare_keys(a: Key, b: Key) -> int:
    """§ 6 as a comparator: negative when `a` is the more specific, so sorting
    with it puts the most specific first. The kind slot first, then each later
    segment, where a value beats an empty or omitted one. Keys that never overlap
    order by spelling, so the order is total."""
    if a.rank != b.rank:
        return b.rank - a.rank
    if a.slot != b.slot:
        return -1 if a.slot < b.slot else 1
    for i in range(max(len(a.segs), len(b.segs))):
        va = a.segs[i] if i < len(a.segs) else ""
        vb = b.segs[i] if i < len(b.segs) else ""
        if va == vb:
            continue
        if va and not vb:
            return -1
        if vb and not va:
            return 1
        return -1 if va < vb else 1
    return 0


most_specific_first = functools.cmp_to_key(compare_keys)


def matches(key: Key, probe: Probe) -> bool:
    """Does a key match a probe insight?"""
    if probe.kind not in key.kinds:
        return False
    if key.scope.type == "all":
        return True
    if key.scope.type in ("metric", "d"):
        return probe.scope == key.scope
    return probe.scope == Scope("unchecked", key.norm)


def covers(row: Key, key: Key) -> bool:
    """Does `row` match every insight `key` could match (Duty Menus § 6.2 step
    1), for the shapes the engine reads? An unchecked key is covered only by a
    row over all of its kinds, or by itself."""
    if not set(key.kinds) <= set(row.kinds):
        return False
    if row.scope.type == "all":
        return True
    if row.scope.type == "unchecked":
        return row.norm == key.norm
    return key.scope == row.scope


def _meet_of(key: Key, kind: int) -> Key | None:
    """A key narrowed to one kind (a meet): the kind in its slot, its later
    segments kept."""
    return parse_key(":".join([str(kind), *key.segs]))


def _profiles_spelling(key: Key) -> bool:
    """A `30392` or `30382` `tag` or `pin` key with an empty category and a value
    after it, which § 4.4 reads as the Profiles category alone."""
    return (
        key.slot in ("30392", "30382")
        and len(key.segs) >= 3
        and key.segs[0] in ("tag", "pin")
        and key.segs[1] == ""
    )


T = TypeVar("T")


def winner_of(
    candidates: Iterable[T], probe: Probe, key_of: Callable[[T], Key]
) -> T | None:
    """The most specific of `candidates` matching the probe, or None."""
    best: T | None = None
    for candidate in candidates:
        key = key_of(candidate)
        if not matches(key, probe):
            continue
        if best is None or compare_keys(key, key_of(best)) < 0:
            best = candidate
    return best


def build_probes(keys: Iterable[Key]) -> list[Probe]:
    """The finite set of probes for the keys given: per metric named, and the
    published ones, for each Score kind; one per List kind; per `d` named, and
    one other, for each Concept kind; one per unchecked key and kind it covers;
    and one for any other kind a key names."""
    parsed = list(keys)
    out: dict[Probe, None] = {}
    for kind in SCORE_KINDS:
        metrics = list(PUBLISHED_SCORE_METRICS) if kind == SCORE_KIND else []
        for k in parsed:
            if (
                k.scope.type == "metric"
                and kind in k.kinds
                and k.scope.value not in metrics
            ):
                metrics.append(k.scope.value)
        for metric in metrics:
            out.setdefault(Probe(kind, Scope("metric", metric)))
        out.setdefault(Probe(kind, Scope("other")))
    for kind in LIST_KINDS:
        out.setdefault(Probe(kind, Scope("other")))
    for kind in CONCEPT_KINDS:
        for k in parsed:
            if k.scope.type == "d" and kind in k.kinds:
                out.setdefault(Probe(kind, Scope("d", k.scope.value)))
        out.setdefault(Probe(kind, Scope("other")))
    for k in parsed:
        if k.scope.type == "unchecked":
            for kind in k.kinds:
                out.setdefault(Probe(kind, Scope("unchecked", k.norm)))
        for kind in k.kinds:
            if not (is_score_kind(kind) or is_list_kind(kind) or is_concept_kind(kind)):
                out.setdefault(Probe(kind, Scope("other")))
    return list(out)


# --- The reader's Map -----------------------------------------------------------


def _tags_of(event: Any) -> list:
    tags = event.get("tags") if isinstance(event, dict) else None
    return tags if isinstance(tags, list) else []


@dataclass(eq=False)
class Group:
    """One key of a Map, with the providers it names. A set: Preferred and
    Alternate carry the same duty (§ 9), so their order isn't kept, and one key
    of a 1 MiB Map can name some 12,000, too many to scan per tag or probe."""

    key: Key
    providers: set[str] = field(default_factory=set)


def map_groups(event: Any) -> list[Group]:
    """A Map's entries, one group per key in the order the Map first names it.
    Tags whose key doesn't parse, or whose element 2 isn't a key, are left out."""
    by_norm: dict[str, Group] = {}
    for tag in _tags_of(event):
        if not isinstance(tag, list) or len(tag) < 2 or not isinstance(tag[1], str):
            continue
        parsed = parse_key(tag[0])
        if parsed is None or not HEX64.fullmatch(tag[1]):
            continue
        by_norm.setdefault(parsed.norm, Group(parsed)).providers.add(tag[1].lower())
    return list(by_norm.values())


# A Map is checked whole or not at all: a check weighs each key against every
# other, so its cost grows with the square of the keys. `/setup` writes eight;
# at 64 the worst shape costs about 40 ms. Never truncated: a cut Map can give
# the wrong self-entries.
MAX_MAP_KEYS = 64


def too_big_to_check(event: Any) -> bool:
    """Does the Map name more keys than a check reads?"""
    return len(map_groups(event)) > MAX_MAP_KEYS


def in_supported_sets(probe: Probe) -> bool:
    """Can a probe hold an insight of the observer's supported sets (§ 8)? The
    published metrics for 30382; no Concept, since the Assistant curates none
    yet; and, for everything else, assumed so."""
    if probe.scope.type == "unchecked":
        return True
    if probe.kind == SCORE_KIND:
        return (
            probe.scope.type == "metric"
            and probe.scope.value in PUBLISHED_SCORE_METRICS
        )
    return not is_concept_kind(probe.kind)


@dataclass(frozen=True)
class Duty:
    probe: Probe
    # The Map's most specific group matching the probe (§ 6).
    winner: Group | None
    # Whether the probe is the Assistant's duty (§ 9).
    duty: bool


def duties_of(
    groups: list[Group], assistant: str, probes: list[Probe]
) -> dict[Probe, Duty]:
    """For each probe, the Map's winning group, and whether the probe is the
    Assistant's duty: its winning key names the Assistant, as Preferred or
    Alternate (§ 9), and, under a scope key, the probe is in the supported sets."""
    who = assistant.lower()
    out: dict[Probe, Duty] = {}
    for probe in probes:
        winner = winner_of(groups, probe, lambda g: g.key)
        duty = (
            winner is not None
            and who in winner.providers
            and (winner.key.scope.type != "all" or in_supported_sets(probe))
        )
        out[probe] = Duty(probe, winner, duty)
    return out


# --- The offer and what is live ---------------------------------------------------


@dataclass(frozen=True)
class OfferRow:
    key: Key
    relay: str


class Offer:
    """The Duty Menu's offer (`duty_menu_build.offer_rows`, `[key, relay]` per
    row, every one a `duty` row with a relay and a key of its own), read by Duty
    Menus § 6.2."""

    def __init__(self, rows: Iterable[list[str]]):
        self.rows: list[OfferRow] = []
        for key, relay in rows:
            parsed = parse_key(key)
            if parsed is None:
                raise ValueError(f"an offer row's key doesn't parse: {key!r}")
            self.rows.append(OfferRow(parsed, relay))
        self.kinds = {kind for row in self.rows for kind in row.key.kinds}

    def answer(self, probe: Probe) -> OfferRow | None:
        """The row answering a probe's insights: the most specific that matches
        them (§ 6.2 step 2); None when no row does, so they aren't offered."""
        return winner_of(self.rows, probe, lambda r: r.key)

    def covering(self, key: Key) -> OfferRow | None:
        """The row answering a key as a whole: the most specific covering it
        (§ 6.2 steps 1–3); None when no row covers it, so the offer gives the
        key no relay."""
        best: OfferRow | None = None
        for row in self.rows:
            if covers(row.key, key) and (
                best is None or compare_keys(row.key, best.key) < 0
            ):
                best = row
        return best

    def order_of(self, key: Key) -> int:
        """Where a key's covering row stands in the offer, for listing keys in its order."""
        row = self.covering(key)
        return self.rows.index(row) if row is not None else len(self.rows)


@dataclass(frozen=True)
class ScoresUpkeep:
    """Whether the Assistant's Scores have landed and are kept up to date:
    `live`, `not-landed`, `not-scheduled` or `upkeep-stopped`, and, when they
    have stopped, the last publish in seconds."""

    status: str
    last_landed_at: int | None = None


def scores_upkeep(
    last_published: datetime | None, interval_seconds: int | None, now: datetime
) -> ScoresUpkeep:
    """Live when the last publish (naive UTC; set only when the relay OK'd every
    event of a run) is no older than two schedule intervals: one missed run is a
    brief gap, two in a row is a stop. `interval_seconds` is None when nothing
    keeps them up to date."""
    if not interval_seconds or interval_seconds <= 0:
        return ScoresUpkeep("not-scheduled")
    if last_published is None:
        return ScoresUpkeep("not-landed")
    if (now - last_published).total_seconds() > 2 * interval_seconds:
        landed = int(last_published.replace(tzinfo=timezone.utc).timestamp())
        return ScoresUpkeep("upkeep-stopped", landed)
    return ScoresUpkeep("live")


@dataclass(frozen=True)
class Live:
    live: bool
    relay: str | None = None
    # Why an offered part isn't live.
    reason: str | None = None
    last_landed_at: int | None = None


def evaluate_live(probe: Probe, offer: Offer, scores: ScoresUpkeep) -> Live:
    """Is a probe live, and if not why? Not offered when no offer row answers it."""
    row = offer.answer(probe)
    if row is None:
        return Live(live=False)

    def no(reason: str, last_landed_at: int | None = None) -> Live:
        return Live(False, row.relay, reason, last_landed_at)

    if probe.scope.type == "unchecked":
        return no("unchecked")
    if is_score_kind(probe.kind):
        # The one guard against claiming a Score Brainstorm doesn't publish.
        if (
            probe.kind != SCORE_KIND
            or probe.scope.type != "metric"
            or probe.scope.value not in PUBLISHED_SCORE_METRICS
        ):
            return no("not-published-here")
        if scores.status == "live":
            return Live(True, row.relay)
        return no(scores.status, scores.last_landed_at)
    if is_list_kind(probe.kind):
        # Published only when an admin runs POST /admin/trustedLists.
        return no("not-scheduled")
    return no("not-published-here")


# --- What the Assistant's Treasure Map should list ---------------------------------


def build_should_say(
    groups: list[Group],
    assistant: str,
    duties: dict[Probe, Duty],
    live: dict[Probe, Live],
    offer: Offer,
) -> list[tuple[Key, str]]:
    """The self-entries, `(key, relay)` (ADR 0001 sub-decision 2): deterministic
    and greedy, most specific first. Each of the reader's keys naming the
    Assistant is tried verbatim, then its meets with each offered kind it
    covers, then, for a `30382` meet, one `30382:<metric>` per published metric
    that is a duty under it. A candidate at relay R is valid when every duty it
    would win among the self-entries chosen so far is live at R, and it wins one.
    Nothing else is copied from the reader's Map (§ 9.2 "No order, no copies").

    Under today's `offer_rows`, only Scores are ever live, so this can list only
    the five `30382:<metric>` keys. It stays general for when Lists and Concepts
    become live duties (NosFabrica/brainstorm_server#112)."""
    who = assistant.lower()
    mine = sorted(
        (g.key for g in groups if who in g.providers), key=most_specific_first
    )
    every = list(duties.values())
    chosen: list[tuple[Key, str]] = []

    def has(norm: str) -> bool:
        return any(key.norm == norm for key, _ in chosen)

    def valid(candidate: Key, relay: str) -> bool:
        speaks = False
        for d in every:
            if not d.duty or not matches(candidate, d.probe):
                continue
            if (
                winner_of([*(k for k, _ in chosen), candidate], d.probe, lambda k: k)
                is not candidate
            ):
                continue
            state = live.get(d.probe)
            if state is None or not state.live or state.relay != relay:
                return False
            speaks = True
        return speaks

    def try_key(candidate: Key | None) -> bool:
        if candidate is None or has(candidate.norm):
            return False
        row = offer.covering(candidate)
        if row is None or not valid(candidate, row.relay):
            return False
        chosen.append((candidate, row.relay))
        return True

    for k in mine:
        if has(k.norm) or try_key(k):
            continue
        meets: list[Key] = []
        for kind in k.kinds:
            meet = _meet_of(k, kind) if kind in offer.kinds else None
            if meet is not None and all(m.norm != meet.norm for m in meets):
                meets.append(meet)
        for meet in sorted(meets, key=most_specific_first):
            if meet.norm != k.norm and try_key(meet):
                continue
            if meet.kinds != (SCORE_KIND,) or meet.scope.type != "all":
                continue
            for metric in PUBLISHED_SCORE_METRICS:
                d = duties.get(Probe(SCORE_KIND, Scope("metric", metric)))
                if (
                    d
                    and d.duty
                    and d.winner is not None
                    and d.winner.key.norm == k.norm
                ):
                    try_key(parse_key(f"{SCORE_KIND}:{metric}"))
    # Listed in the offer's order; order never counts (§ 9.2).
    chosen.sort(key=lambda c: offer.order_of(c[0]))
    return chosen


@dataclass(frozen=True)
class DutyLine:
    """One part of one of the reader's entries naming the Assistant."""

    key: str
    # `live`, `offered` (not live, with a reason), `not-offered`, or `elsewhere`
    # (a more specific entry gives it to another key).
    status: str
    relay: str | None = None
    reason: str | None = None
    last_landed_at: int | None = None
    # The should-say entries that list this part: empty unless it's live.
    entries: tuple[tuple[str, str], ...] = ()


def _line_key(key: Key, row: Key) -> str:
    """A part's key: the meet of the reader's key with the row answering the
    part, or the reader's own key where the meet would be a narrower spelling
    than the part (§ 4.4's Profiles category)."""
    if covers(row, key):
        return key.norm
    if covers(key, row):
        return row.norm
    common = [kind for kind in key.kinds if kind in row.kinds]
    meet = _meet_of(key, common[0]) if len(common) == 1 else None
    if meet is None or (_profiles_spelling(meet) and not _profiles_spelling(key)):
        return key.norm
    return meet.norm


def _part_line(
    key: Key,
    row: OfferRow,
    part: list[Duty],
    live: dict[Probe, Live],
    should: list[tuple[Key, str]],
) -> DutyLine:
    line_key = _line_key(key, row.key)
    on_duty = [d.probe for d in part if d.duty]
    if not on_duty:
        first = live[part[0].probe]
        reason = "nothing-to-publish" if first.live else first.reason
        return DutyLine(line_key, "offered", row.relay, reason)
    down = next((live[p] for p in on_duty if not live[p].live), None)
    if down is not None:
        return DutyLine(
            line_key, "offered", row.relay, down.reason, down.last_landed_at
        )
    entries: list[tuple[str, str]] = []
    for probe in on_duty:
        winner = winner_of(should, probe, lambda e: e[0])
        if winner is None or winner[1] != live[probe].relay:
            # A live part nothing should say lists: none, so it never reads as Active.
            entries = []
            break
        if (winner[0].norm, winner[1]) not in entries:
            entries.append((winner[0].norm, winner[1]))
    return DutyLine(line_key, "live", row.relay, entries=tuple(entries))


def duty_lines(
    groups: list[Group],
    assistant: str,
    duties: dict[Probe, Duty],
    live: dict[Probe, Live],
    offer: Offer,
    should: list[tuple[Key, str]],
) -> list[DutyLine]:
    """The duties lines (ADR 0001 sub-decision 7), flat: for each of the
    reader's keys naming the Assistant, in Map order, a line per offer row that
    answers part of what the key wins, in the offer's order; a `not-offered`
    line when a duty under it has no row; and an `elsewhere` line per more
    specific key naming only others that wins some of it."""
    who = assistant.lower()
    every = list(duties.values())
    lines: list[DutyLine] = []
    for group in groups:
        if who not in group.providers:
            continue
        key = group.key
        won = [d for d in every if d.winner is group]
        for row in offer.rows:
            part = [d for d in won if offer.answer(d.probe) is row]
            if part:
                lines.append(_part_line(key, row, part, live, should))
        if any(d.duty and offer.answer(d.probe) is None for d in won):
            lines.append(DutyLine(key.norm, "not-offered"))
        elsewhere: list[Group] = []
        for d in every:
            w = d.winner
            if (
                w is None
                or w is group
                or who in w.providers
                or not matches(key, d.probe)
                or compare_keys(w.key, key) >= 0
            ):
                continue
            if w not in elsewhere:
                elsewhere.append(w)
        for w in elsewhere:
            line = DutyLine(w.key.norm, "elsewhere")
            if line not in lines:
                lines.append(line)
    return lines


@dataclass(frozen=True)
class SelfMapCheck:
    duties: list[DutyLine]
    should_say: list[tuple[str, str]]


def evaluate_self_map(
    map_event: Any, offer_rows: list[list[str]], assistant: str, scores: ScoresUpkeep
) -> SelfMapCheck:
    """What the Assistant's Treasure Map should list for the reader whose newest
    kind 10040 is `map_event` (None when they have none), and why."""
    offer = Offer(offer_rows)
    groups = map_groups(map_event)
    probes = build_probes(g.key for g in groups)
    duties = duties_of(groups, assistant, probes)
    live = {probe: evaluate_live(probe, offer, scores) for probe in probes}
    should = build_should_say(groups, assistant, duties, live, offer)
    return SelfMapCheck(
        duty_lines(groups, assistant, duties, live, offer, should),
        [(key.norm, relay) for key, relay in should],
    )


# --- Reading and writing the Assistant's Treasure Map ---------------------------------


def self_map_relays(
    outbox: list[str], should_say: list[tuple[str, str]], hints: list[str]
) -> list[str]:
    """Where a version is sent and looked for (§ 9.2 "Finding a self-Map"): the
    Assistant's kind-10002 write relays, every relay a should-say entry names,
    and the reader's hints for the Assistant; each once, in that order."""
    out: list[str] = []
    seen: set[str] = set()
    for url in [*outbox, *(relay for _, relay in should_say), *hints]:
        if not isinstance(url, str) or not RELAY_URL.fullmatch(url.strip()):
            continue
        if relay_key(url) not in seen:
            seen.add(relay_key(url))
            out.append(url.strip())
    return out


def newest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """NIP-01's newest: the latest `created_at`, and on a tie the lowest id."""
    return min(
        events,
        key=lambda e: (-int(e.get("created_at") or 0), str(e.get("id") or "")),
        default=None,
    )


def read_self_entries(event: Any, author: str) -> list[tuple[str, str]]:
    """A version's self-entries, `(key, relay)`: its tags whose key parses and
    whose element 2 is the author, each once. The relay is element 3 as written
    (trimmed), `""` when missing or empty, which claims nothing."""
    who = author.lower()
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in _tags_of(event):
        if not isinstance(tag, list) or len(tag) < 2 or not isinstance(tag[1], str):
            continue
        parsed = parse_key(tag[0])
        if parsed is None or tag[1].lower() != who:
            continue
        relay = tag[2].strip() if len(tag) > 2 and isinstance(tag[2], str) else ""
        if (parsed.norm, relay_key(relay)) not in seen:
            seen.add((parsed.norm, relay_key(relay)))
            out.append((parsed.norm, relay))
    return out


def same_self_entries(a: list[tuple[str, str]], b: list[tuple[str, str]]) -> bool:
    """Two lists of self-entries as sets of key and relay: order never counts."""
    return {(k, relay_key(r)) for k, r in a} == {(k, relay_key(r)) for k, r in b}


def self_entry_relays(events: list[Any], author: str) -> list[str]:
    """Every relay that the entries naming `author` in `events` give (element 3):
    a version's self-entries, so a new version replaces it there too (§ 9.2
    "Finding a self-Map"), or a reader's hints for its Assistant."""
    return [
        relay
        for event in events
        for _, relay in read_self_entries(event, author)
        if RELAY_URL.fullmatch(relay)
    ]


def nothing_to_change(
    latest: dict[str, Any] | None, author: str, target: list[tuple[str, str]]
) -> bool:
    """Would a new version change nothing? Its self-entries already are the target."""
    return same_self_entries(read_self_entries(latest, author), target)


def self_map_tags(author: str, entries: list[tuple[str, str]]) -> list[list[str]]:
    """A version's tags, before the server's `client` tag: the self-entries,
    `[key, author, relay]`, and nothing else. Only this server signs the
    Assistant's kind 10040 (no route exports its key), so a version holds no
    other tag to keep, and its content is empty. Should the key ever be
    exported, Tapestry's merge (ADR `assistant-self-map/0002` sub-decision 4)
    comes back: every other tag and the content kept as found."""
    who = author.lower()
    return [[key, who, relay] for key, relay in entries]
