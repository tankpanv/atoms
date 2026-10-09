from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from .db import initialize
from .routes import router
from .routers.system import router as system_router

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(system_router)
DIST = Path(__file__).resolve().parents[2] / "dist"

@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API not found")
    candidate = (DIST / path).resolve()
    if not candidate.is_relative_to(DIST.resolve()):
        raise HTTPException(404)
    if candidate.is_file():
        return FileResponse(candidate)
    if path.startswith("assets/") or not (DIST / "index.html").is_file():
        raise HTTPException(404)
    return FileResponse(DIST / "index.html")
