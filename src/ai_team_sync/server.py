"""FastAPI application for ai-team-sync."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ai_team_sync.database import init_db
from ai_team_sync.config import settings
from ai_team_sync.routers import sessions, locks, decisions, override_requests, git_status, websocket, dashboard, presence_ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Start background tasks
    from ai_team_sync.background_tasks import start_background_tasks
    await start_background_tasks()

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="ai-team-sync",
        description="Change management API for AI-assisted development teams",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(sessions.router, prefix="/api")
    app.include_router(locks.router, prefix="/api")
    app.include_router(decisions.router, prefix="/api")
    app.include_router(override_requests.router, prefix="/api")
    app.include_router(git_status.router, prefix="/api")
    app.include_router(websocket.router)
    app.include_router(dashboard.router)
    app.include_router(presence_ws.router)

    # Serve extension download
    project_root = Path(__file__).resolve().parent.parent.parent
    vsix = project_root / "vscode-extension"
    if vsix.exists():
        app.mount("/ext", StaticFiles(directory=str(vsix)), name="extension")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "ai-team-sync"}

    return app


app = create_app()


def main():
    uvicorn.run(
        "ai_team_sync.server:app",
        host=settings.ats_host,
        port=settings.ats_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
