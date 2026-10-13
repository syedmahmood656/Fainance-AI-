from app.database.models import SessionLocal  # re-exported for convenience


def get_db():
    """FastAPI-style dependency (kept for API routes; the agent uses `with SessionLocal() as s:`)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
