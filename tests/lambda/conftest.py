import pytest

from .helpers import ENV


@pytest.fixture
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return ENV
