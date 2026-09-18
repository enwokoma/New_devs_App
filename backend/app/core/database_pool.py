import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings

logger = logging.getLogger(__name__)


def _async_database_url(url: str) -> str:
    """
    The compose file provides a plain ``postgresql://`` URL (psycopg2 style).
    The async engine needs the asyncpg driver, so normalise the scheme here.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    return url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def initialize(self):
        """Initialize database connection pool (idempotent)."""
        if self.session_factory is not None:
            return

        try:
            # BUG FIX: this used to build the URL from ``settings.supabase_db_*``
            # attributes that do not exist on Settings, so initialisation always
            # raised AttributeError and the revenue service silently fell back
            # to hard-coded mock figures. Use the configured DATABASE_URL.
            database_url = _async_database_url(settings.database_url)

            # NOTE: async engines must use the default AsyncAdaptedQueuePool;
            # passing the sync ``QueuePool`` raises at engine creation.
            self.engine = create_async_engine(
                database_url,
                pool_size=20,
                max_overflow=30,
                pool_pre_ping=True,
                pool_recycle=3600,
                echo=False,
            )

            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            logger.info("✅ Database connection pool initialized")

        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
            raise

    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    def get_session(self) -> AsyncSession:
        """
        Get a database session from the pool.

        BUG FIX: this was declared ``async def`` while callers used
        ``async with db_pool.get_session()``. An ``async def`` returns a
        coroutine, which is not an async context manager, so every call
        raised and pushed the service into its mock-data fallback.
        """
        if not self.session_factory:
            raise RuntimeError("Database pool not initialized")
        return self.session_factory()


# Global database pool instance (shared across requests instead of creating a
# new engine per request).
db_pool = DatabasePool()


async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        yield session
