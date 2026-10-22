"""Settings boot: removed env keys are tolerated, a short session secret only warns."""

from unittest.mock import MagicMock

from app.core.config import Settings
from app.utils.auth import auth_util


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


def test_boots_with_a_session_secret_under_32_bytes():
    assert Settings(auth_secret_key="x" * 31).auth_secret_key == "x" * 31


def test_warns_at_startup_when_the_session_secret_is_under_32_bytes(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(auth_util, "logger", logger)
    secret = "s3cr3t-value-" + "x" * 18

    auth_util.warn_if_session_secret_short(secret)

    logger.warning.assert_called_once()
    message = " ".join(str(arg) for arg in logger.warning.call_args.args)
    assert "shorter than 32 bytes" in message
    assert secret not in message
    assert "31" not in message


def test_no_warning_for_a_32_byte_session_secret(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(auth_util, "logger", logger)

    auth_util.warn_if_session_secret_short("x" * 32)

    logger.warning.assert_not_called()
