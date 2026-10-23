from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .config import settings
from .database import Base, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Local SQLite convenience; production startup should run `alembic upgrade head`.
    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="alg-qa-agent",
    version="0.1.0",
    description="商家业务域 AI 知识治理平台与 Agent Runtime",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix=settings.api_prefix)


@app.get("/")
def root() -> dict:
    return {
        "service": settings.app_name,
        "docs": "/docs",
        "health": f"{settings.api_prefix}/health",
    }
