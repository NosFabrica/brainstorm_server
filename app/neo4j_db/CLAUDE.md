# app/neo4j_db

Neo4j async driver bootstrap. **Only the driver lives here**; the Cypher queries are in [../repos/user_repo.py](../repos/user_repo.py). Don't put queries in this folder.

## Files

| File | Purpose |
|---|---|
| `driver.py` | Module-level `driver = AsyncGraphDatabase.driver(...)` singleton. Connects on import. Exports `test_neo4j_driver()` (used by `app/api.py` lifespan to verify connectivity at startup). |
| `__init__.py` | Empty. |

## Usage pattern

Other modules import the driver and open a session per logical operation:

```python
from app.neo4j_db.driver import driver
from app.repos.user_repo import get_user_graph_data

async with driver.session() as session:
    data = await get_user_graph_data(session, pubkey, influence_key)
```

The session is the unit of work, not the driver. **One session per request scope** is the right granularity; long-lived sessions across requests will leak transactions.

## Where do queries live?

[../repos/user_repo.py](../repos/user_repo.py). All ~19 Cypher functions, including the composite ones (`get_outbound_counts_and_influence`, `get_all_section_stats`, `get_paginated_section_connections`). Refer to that file's section in [../repos/CLAUDE.md](../repos/CLAUDE.md) for the catalog.

## Adding a new query

1. Add an `async def` in `app/repos/user_repo.py`. Take the session as the first arg.
2. Parametrize **everything** that could be user input. Dynamic property keys → `node[$key]`, not f-string.
3. If you build a write query, wrap your call site in `session.execute_write(lambda tx: tx.run(...))` so it picks the leader.

## Gotchas

- `AsyncGraphDatabase.driver(...)` does *not* connect eagerly — `test_neo4j_driver()` is what surfaces a missing Neo4j at startup.
- `await driver.close()` is intentionally **not** called in lifespan teardown today — the process exits and lets the driver cleanup happen on its own. If you add graceful shutdown, mirror what `vespa.aclose()` does in `app/api.py`.
- The driver's URL / auth come from `settings.neo4j_db_url` / `neo4j_db_username` / `neo4j_db_password` in [`app/core/config.py`](../core/config.py).
