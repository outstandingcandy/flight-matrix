"""
Base Service Module

Provides a base class for all service classes with common session management
patterns to eliminate boilerplate code.
"""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from src.utils.database import DatabaseManager


class BaseService:
    """Base class for services that interact with the database.

    Provides context managers for session handling, reducing boilerplate
    try-finally blocks in derived classes.

    Attributes:
        db: Database manager instance
    """

    def __init__(self, db: DatabaseManager):
        """Initialize the base service.

        Args:
            db: Database manager instance for database operations
        """
        self.db = db

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Transactional session scope - auto commit/rollback/close.

        Use this for operations that modify data and need transaction
        semantics.

        Yields:
            SQLAlchemy Session

        Example:
            with self.session_scope() as session:
                user = User(email='test@example.com')
                session.add(user)
                # commit happens automatically on exit
        """
        session = self.db.get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def readonly_session(self) -> Generator[Session, None, None]:
        """Read-only session scope - no commit, just close.

        Use this for read operations that don't modify data.

        Yields:
            SQLAlchemy Session

        Example:
            with self.readonly_session() as session:
                users = session.query(User).all()
                return users
        """
        session = self.db.get_session()
        try:
            yield session
        finally:
            session.close()
