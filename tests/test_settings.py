"""Settings tolerate env keys for settings that were removed."""

from app.core.config import Settings


def test_boots_from_a_dotenv_still_setting_the_removed_vespa_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "VESPA_URL=http://localhost:8080\n"
        "VESPA_FULL_SYNC=false\n"
        "VESPA_SWEEP_BELOW_CUTOFF=false\n"
        "NIP50_BACKING_RELAY_URL=ws://localhost:7777\n"
    )

    settings = Settings(_env_file=env)

    assert not hasattr(settings, "vespa_url")
