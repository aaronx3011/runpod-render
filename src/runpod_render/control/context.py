"""Process-wide dependencies shared by the API and the scheduler."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .db import make_engine, make_sessionmaker
from .runpod import RunPodClient
from .settings import Settings
from .storage import Storage


@dataclass
class Context:
    settings: Settings
    engine: Engine
    sessions: sessionmaker
    storage: Storage
    runpod: RunPodClient

    @classmethod
    def build(cls, settings: Settings, *, engine: Engine | None = None, storage: Storage | None = None,
              runpod: RunPodClient | None = None) -> Context:
        engine = engine or make_engine(settings.database_url)
        return cls(settings=settings, engine=engine, sessions=make_sessionmaker(engine),
                   storage=storage or Storage(settings),
                   runpod=runpod or RunPodClient(settings.runpod_api_base, settings.runpod_api_key,
                                                 timeout_s=settings.runpod_timeout_s))
