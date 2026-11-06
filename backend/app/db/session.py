from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine = None
_SessionLocal = None


def _factory() -> sessionmaker:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        # Supabase's transaction pooler (port 6543) does not support prepared statements.
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5,
                                max_overflow=5, connect_args={"prepare_threshold": None})
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = _factory()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Worker helper: commit on success, roll back on error."""
    db = _factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
