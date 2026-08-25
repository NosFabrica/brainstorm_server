from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_url: str = Field(...)
    deploy_environment: str = Field(...)
    auth_algorithm: str = Field(...)
    auth_secret_key: str = Field(...)
    auth_access_token_expire_minutes: int = Field(...)
    sql_admin_username: str = Field(...)
    sql_admin_password: str = Field(...)
    sql_admin_secret_key: str = Field(...)
    neo4j_db_url: str = Field(...)
    neo4j_db_username: str = Field(...)
    neo4j_db_password: str = Field(...)
    redis_host: str = Field(...)
    redis_port: str = Field(...)
    nostr_transfer_from_relay: str = Field(...)
    nostr_transfer_to_relay: str = Field(...)
    nostr_transfer_to_relay2: str | None = Field(None)
    nostr_upload_ta_events_relay: str = Field(...)
    nostr_upload_ta_events_relay_public_url: str = Field(...)
    cutoff_of_valid_graperank_scores: float = Field(...)
    perform_nostr_full_sync: bool = Field(...)
    frontend_url: str = Field(...)
    public_base_url: str = Field(...)
    admin_enabled: bool = Field(default=False)
    admin_whitelisted_pubkeys: str = Field(default="")
    stale_ongoing_brainstorm_request_threshold_hours: float = Field(default=7.0)
    stale_ongoing_brainstorm_request_check_interval_minutes: float = Field(default=30.0)
    block_frequent_graperank_requests: bool = Field(default=False)
    block_frequent_graperank_requests_minutes: int = Field(default=30)
    # Global kill-switch for the tier scheduler. Default off; enable per environment
    scheduler_enabled: bool = Field(default=False)
    # Max scheduled runs whose publishing may be in flight before admission pauses.
    scheduler_inflight_target: int = Field(default=1)
    # Priority-weighted admission (w=priority+1). Off = strict highest-first.
    scheduler_fairness_enabled: bool = Field(default=False)
    periodic_graperank_pubkey: str = Field(default="")
    vespa_url: str = Field(...)
    # Per-sink publish mode. False (default) = only changed scores; True =
    # re-assert every above-cutoff score each run. Re-assertion only, not deletes.
    vespa_full_sync: bool = Field(default=False)  # Vespa upserts
    relay_full_sync: bool = Field(default=False)  # kind-30382 TA republish
    # Per-sink reaping: also delete EVERY below-cutoff Observee, not just
    # those that fell off the baseline. Backwards-compat only — drains the legacy
    # pre-cutoff backlog, then off. Not in env.example
    relay_sweep_below_cutoff: bool = Field(default=False)
    vespa_sweep_below_cutoff: bool = Field(default=False)
    # Count-gated parallel signing. At/below the threshold a publish run signs
    # via the simple sequential client loop (zero pool overhead — the common,
    # steady-state case). Above it, a *large* burst (first publish for an
    # Observer, big graph shift) shards signing across a ProcessPoolExecutor,
    # which both ~10×-speeds the sign and keeps the GIL-holding nostr-sdk signing
    # off the event loop so concurrent requests aren't starved.
    sign_parallel_threshold: int = Field(default=10_000)
    sign_parallel_max_workers: int | None = Field(default=None)
    # --- Trusted Lists (kind-30392 from kind-39999 taggings) ---------------
    # Inclusive floor on the asserter's Rank (0-100) in the Observer's own web
    # of trust. Default 3 is exactly issue #73's "rank > 2" — stated as an
    # inclusive floor so the comparison has no off-by-one. Compared against raw
    # Influence (rank/100) to avoid double-rounding against published Rank.
    trusted_list_min_rank: int = Field(default=3)
    # How many distinct qualifying asserters must have used a Tag for it to
    # enter the dictionary. Issue #73 anticipates raising this, so it is not a
    # literal.
    trusted_list_min_tag_uses: int = Field(default=1)
    # Membership predicate floor: applications >= cutoff (and > disputes).
    trusted_list_cutoff: int = Field(default=1)
    # Where TLs are published. Empty = fall back to the TA relay, so retargeting
    # to the nip85.* relays is an env change rather than a diff.
    trusted_list_relay: str = Field(default="")
    # Published-state-drift backstop: every Nth *scheduled* run for an observer
    # forces a full re-assert on both sinks.
    # <= 0 disables the backstop.
    full_sync_every_n_runs: int = Field(default=84)
    # Internal strfry relay used as the source of original signed kind-0 events
    # for the NIP-50 /relay endpoint. Not exposed externally — the FastAPI
    # /relay handler proxies search results out, never raw client traffic.
    nip50_backing_relay_url: str = Field(default="ws://localhost:7777")
    nip50_strfry_timeout_seconds: float = Field(default=3.0)
    # Open Ranking (ORE) auth posture. When True, every data endpoint requires
    # a valid NWT (ORE-A) and answers ONLY from the signer's own observer
    # perspective (a client-supplied `pov` is ignored). When False (default),
    # the endpoints are open/unauthenticated and use the public global observer
    # plus any client-supplied `pov` per ORE-01.
    open_ranking_require_auth: bool = Field(default=False)


settings = Settings()
