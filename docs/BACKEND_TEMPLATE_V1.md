# Backend template v1

The downloaded StyleMate project is a useful speed reference, but its actual architecture is a frontend-only demo: localStorage stores quota, `@metagptx/web-sdk` handles storage/AI, and its architecture document says there are no database tables. Its bundled FastAPI modules are a generic platform starter and the project progress says upload/AI generation was not fully verified.

Our `fullstack-v1` template already included a FastAPI/PostgreSQL connector. It is now a modular starter without copying the unused cloud-specific backend:

- `app/db.py` keeps the managed project Schema and restricted application role.
- `app/config.py` provides typed environment settings without putting secrets in source.
- `app/routers/` holds system and bounded-domain HTTP routers.
- `app/schemas/` holds Pydantic request/response contracts.
- `app/services/` holds infrastructure and domain rules.
- `/api/health` is liveness; `/api/ready` performs a real database readiness query.
- `backend/README.md` documents the vertical-slice flow: schema → service → route → UI.

The template still uses the platform's synchronous `psycopg` connector and per-project PostgreSQL Schema. This is deliberate: the reference project's SQLAlchemy/asyncpg/Lambda/payment/auth stack would add dependencies and startup surface without helping a simple generated product. Add those modules only when the requested product needs them.

The template manifest version is `2026.10.06.1`. New full-stack projects receive it automatically; existing projects are never overwritten.
