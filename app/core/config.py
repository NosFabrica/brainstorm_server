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
    # Flash payments. Off by default so self-hosted and one-click deployments —
    # which have no Flash account — never mount the surface at all. Enabling it
    # without credentials refuses to start (see flash_webhook_service).
    flash_enabled: bool = Field(default=False)
    flash_base_url: str = Field(default="https://dev.server.vault.paywithflash.com")
    flash_api_key: str = Field(default="")
    flash_webhook_secret: str = Field(default="")
    # Set only during a rotation, so deliveries Flash signed before the swap are
    # still accepted. Clearing it is what ends the window.
    flash_webhook_secret_previous: str = Field(default="")
    # Where checkout sends the user back. Empty = {frontend_url}/billing/return.
    # Must exactly match a URL registered in the Flash dashboard.
    flash_checkout_redirect_url: str = Field(default="")
    # Routes Flash reads to the in-process fake (app/core/flash_mock.py).
    # Test-only; with no Flash sandbox this is how the paid paths are exercised.
    flash_mock_enabled: bool = Field(default=False)
    # Per-attempt HTTP timeout for Flash reads.
    flash_http_timeout_seconds: float = Field(default=5.0)
    # How long a plan read stays fresh. The pricing page is public, so this is
    # what keeps anonymous traffic off Flash's quota; a price change takes this
    # long to appear. The last-known-good copy behind it never expires.
    flash_plans_cache_ttl_seconds: int = Field(default=300)
    # Replay window for webhook signatures, per Flash's own reference handler.
    flash_webhook_tolerance_seconds: int = Field(default=300)
    # Periodic billing reconciliation. Unset = follows flash_enabled; set it
    # explicitly only to stop the sweep on its own. See billing_sync_active.
    billing_sync_enabled: bool | None = Field(default=None)
    billing_sync_interval_seconds: int = Field(default=21600)
    # Subscribers re-read from Flash per cycle. Bounded so a backlog drains
    # steadily instead of hammering their API in one burst.
    billing_reconcile_batch: int = Field(default=25)
    # How long since a subscriber was last read from Flash before we ask again.
    billing_reconcile_stale_after_seconds: int = Field(default=21600)
    # A checkout that never confirmed and that Flash no longer knows about is
    # abandoned, not pending: stop re-reading it once the failure is this old.
    # Long enough that the answer has to repeat before we act — the only thing
    # this guards against is Flash answering 200 with an empty list for a
    # subscription that exists, and the discard it usually means is not undone.
    # It also gates what the subscriber is shown, so longer is not free.
    #
    # Deliberately SHORTER than billing_sync_interval_seconds. The window is a
    # minimum age and the sweep can only act on cycle boundaries, so at exactly
    # one interval a row becomes eligible the instant the cycle evaluating it
    # runs — on staging that race was lost by milliseconds and the row waited
    # another six hours. The margin makes the first eligible cycle deterministic.
    billing_abandon_pending_after_seconds: int = Field(default=18000)
    # Replay of events we acknowledged and then dropped.
    billing_replay_batch: int = Field(default=25)
    # How long a claim is honoured before the event is treated as abandoned.
    billing_replay_stale_after_seconds: int = Field(default=300)
    # Replay attempts before an event is left for a human rather than retried.
    billing_replay_max_attempts: int = Field(default=5)
    # Who may see billing. Empty = fall back to admin_whitelisted_pubkeys.
    billing_admin_whitelisted_pubkeys: str = Field(default="")
    # A subscriber not read from Flash within this is reported as stale.
    billing_stale_sync_hours: int = Field(default=24)
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

    @property
    def billing_sync_active(self) -> bool:
        """Whether the billing reconciliation loop should run.

        Follows `flash_enabled` unless overridden, because the two being
        separately switchable is not a conservative default — a configured Flash
        with the sweep off grants tiers and never takes them back, which leaks
        quietly rather than failing.

        The override exists because `flash_enabled=false` is not a safe way to
        stop the sweep once live: it unmounts the webhook route, so Flash's
        deliveries 404, exhaust their few retries and are lost for good.
        """
        if self.billing_sync_enabled is None:
            return self.flash_enabled
        return self.billing_sync_enabled


settings = Settings()
