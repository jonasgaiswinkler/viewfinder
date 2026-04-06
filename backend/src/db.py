from pathlib import Path

import asyncpg
from dotenv import load_dotenv
import os
from loguru import logger

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

load_dotenv()

POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_HOST = os.getenv("POSTGRES_HOST")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")

logger.info(f"Connecting to database at {POSTGRES_HOST}:{POSTGRES_PORT}, DB: {POSTGRES_DB}, User: {POSTGRES_USER}")

# Replace with your PostgreSQL connection string
# The 'postgresql+asyncpg' prefix tells SQLAlchemy to use asyncpg
DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# Create the asynchronous engine
# pool_pre_ping=True helps in maintaining connections for long-running applications
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

# Configure the asynchronous sessionmaker
# expire_on_commit=False prevents objects from being expired (detached) after a commit,
# which can be useful for keeping objects connected to the session for further operations.
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# Dependency to get an async database session
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


DB_INIT_SQL_PATH = Path(__file__).resolve().parent.parent / "db_init.sql"


async def init_db():
    """Apply db_init.sql on every startup (all statements are idempotent)."""
    if not DB_INIT_SQL_PATH.exists():
        logger.warning(f"db_init.sql not found at {DB_INIT_SQL_PATH}, skipping DB init")
        return

    conn = await asyncpg.connect(
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DB,
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
    )
    try:
        sql = DB_INIT_SQL_PATH.read_text(encoding="utf-8")
        await conn.execute(sql)
        logger.info("Database schema applied from db_init.sql")
    finally:
        await conn.close()