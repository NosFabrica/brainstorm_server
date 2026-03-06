from app.core.loggr import loggr
from neo4j import AsyncGraphDatabase
from app.core.config import settings

logger = loggr.get_logger(__name__)

driver = AsyncGraphDatabase.driver(
    settings.neo4j_db_url, auth=(settings.neo4j_db_username, settings.neo4j_db_password)
)

async def test_neo4j_driver() -> None:
    try:
        await driver.verify_connectivity()
        logger.info("Neo4j is connected!")
        
    except Exception as e:
        logger.error("Neo4j connection is not working!!!")
        # log e if you want
        raise e