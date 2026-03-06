from neo4j import AsyncDriver as AsyncNeoDriver

from app.schemas.schemas import UserConnection


# ----------------- Helper Function -----------------


async def _get_pubkeys_with_influence(
    session: AsyncNeoDriver,
    pubkey: str,
    observer: str | None,
    relation: str,
    direction: str = "incoming",
) -> list[UserConnection]:
    if direction == "incoming":
        match_clause = (
            f"(other:NostrUser)-[:{relation}]->(user:NostrUser {{pubkey: $pubkey}})"
        )
    else:
        match_clause = (
            f"(user:NostrUser {{pubkey: $pubkey}})-[:{relation}]->(other:NostrUser)"
        )

    query = f"""
    MATCH {match_clause}
    RETURN 
        other.pubkey AS pubkey,
        other[$influence_key] AS influence
    """

    influence_key = f"influence_{observer}" if observer else f"influence_{pubkey}"
    result = await session.run(query, pubkey=pubkey, influence_key=influence_key)

    return [
        UserConnection(pubkey=record["pubkey"], influence=record["influence"])
        async for record in result
    ]


# ----------------- Refactored Functions -----------------


# Follows
async def get_list_of_pubkeys_following_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_follows(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="FOLLOWS", direction="outgoing"
    )


# Mutes
async def get_list_of_pubkeys_muting_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_mutes(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="MUTES", direction="outgoing"
    )


# Reports
async def get_list_of_pubkeys_reporting_user(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="incoming"
    )


async def get_list_of_pubkeys_that_user_reports(
    session: AsyncNeoDriver, pubkey: str, observer: str | None = None
) -> list[UserConnection]:
    return await _get_pubkeys_with_influence(
        session, pubkey, observer, relation="REPORTS", direction="outgoing"
    )


# ----------------- Follows -----------------


# Number of users following a given pubkey
async def count_following_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:FOLLOWS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey follows
async def count_user_follows(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:FOLLOWS]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# ----------------- Mutes -----------------


# Number of users muting a given pubkey
async def count_muting_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:MUTES]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey mutes
async def count_user_mutes(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:MUTES]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# ----------------- Reports -----------------


# Number of users reporting a given pubkey
async def count_reporting_user(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (:NostrUser)-[:REPORTS]->(user:NostrUser {pubkey: $pubkey})
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


# Number of users that a given pubkey reports
async def count_user_reports(session: AsyncNeoDriver, pubkey: str) -> int:
    query = """
    MATCH (user:NostrUser {pubkey: $pubkey})-[:REPORTS]->(:NostrUser)
    RETURN COUNT(*) AS count
    """

    result = await session.run(query, pubkey=pubkey)
    record = await result.single()
    return record["count"] if record else 0


async def get_influence_for_observer(
    session: AsyncNeoDriver, pubkey: str, observer_pubkey: str
) -> float | None:
    """
    Returns the value of 'influence_<observer_pubkey>' for the NostrUser with pubkey.
    Returns None if the user or property does not exist.
    """
    property_name = f"influence_{observer_pubkey}"
    query = f"""
    MATCH (user:NostrUser {{pubkey: $pubkey}})
    RETURN user[$property_name] AS influence
    """

    result = await session.run(query, pubkey=pubkey, property_name=property_name)
    record = await result.single()
    return record["influence"] if record and record["influence"] is not None else None
