"""Infrastructure checks used by readiness endpoints and deployment probes."""
from ..db import connection


def database_ready() -> bool:
    with connection() as db:
        db.execute("SELECT 1").fetchone()
    return True
