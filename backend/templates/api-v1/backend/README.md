# Backend starter

This backend is a production-oriented FastAPI starter for Atoms projects. It
uses the managed PostgreSQL connector injected through `APP_DATABASE_URL` and
`APP_DATABASE_SCHEMA`; it never creates a database container or reads platform
admin credentials.

## Layout

- `app/main.py`: application and static frontend mount
- `app/routes.py`: product API router (keep simple routes here during the first slice)
- `app/routers/`: bounded system/domain routers; `system.py` provides `/api/ready`
- `app/schemas/`: Pydantic request and response contracts
- `app/services/`: domain and infrastructure services
- `app/db.py`: schema-isolated transaction connection and initialization hook

Implement one vertical slice at a time: schema -> service -> route -> UI call.
Keep handlers thin, validate input with Pydantic, parameterize SQL with `%s`,
and add tables/migrations in `initialize()` before using them. The health route
is a liveness check; `/api/ready` performs the real database readiness check.

External AI, payment, mail, or storage providers keep a real adapter plus an
explicitly labelled demo adapter when credentials are unavailable. Demo output
never counts as real-provider acceptance; record configuration TODOs and keep
other product flows working.
