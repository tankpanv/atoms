from fastapi import APIRouter
from .db import connection
router = APIRouter(prefix="/api")

@router.get("/health")
def health():
    with connection() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok"}
# Define business schemas and routes here, delegating complex logic to services.
