# Keep in sync with
# brainstorm_graperank_algorithm/src/main/java/com/nosfabrica/graperank/exceptions/ErrorCode.java
from enum import Enum


class ErrorCode(str, Enum):
    # Server's job payload was malformed - missing fields, bad types, or unparseable graperank_params.
    MALFORMED_PARAMS = "MALFORMED_PARAMS"

    # Observer has no (or too few) eligible users in the graph; algorithm has nothing to score.
    NO_ELIGIBLE_USERS = "NO_ELIGIBLE_USERS"

    # Algorithm hit a relationship type it doesn't handle (graph data anomaly).
    UNKNOWN_RELATIONSHIP = "UNKNOWN_RELATIONSHIP"

    # Any Neo4j driver error - unavailable, auth, bad cypher, transient, etc.
    NEO4J_ERROR = "NEO4J_ERROR"

    # Unclassified RuntimeException from the algorithm path - likely a bug.
    ALGORITHM_EXCEPTION = "ALGORITHM_EXCEPTION"

    # Python-only sentinel: code received from Java is not in this enum (Python lagging Java).
    UNKNOWN = "UNKNOWN"
