from app.schemas.error_codes import ErrorCode
from app.schemas.schemas import GrapeRankError


def test_no_eligible_users_is_a_warning_not_our_error():
    error = GrapeRankError.model_validate({"code": "NO_ELIGIBLE_USERS"})
    assert error.model_dump()["severity"] == "warning"


def test_other_codes_are_errors():
    for code in ErrorCode:
        if code is not ErrorCode.NO_ELIGIBLE_USERS:
            assert GrapeRankError(code=code).severity == "error"


def test_stored_severity_is_ignored_and_recomputed():
    error = GrapeRankError.model_validate(
        {"code": "NEO4J_ERROR", "message": None, "severity": "warning"}
    )
    assert error.severity == "error"
