import os

os.environ["DATABASE_URL"] = "sqlite:///./test_alg_qa.db"
os.environ["LLM_MOCK_MODE"] = "true"

import pytest
from app.database import Base, engine
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
