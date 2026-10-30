"""scripts/smoke.py's health wait, on a fake clock."""

import importlib.util
import urllib.request
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "scripts" / "smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_script", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Alive:
    returncode = None

    def poll(self):
        return None


class Healthy:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("ready_at", [45.0, None])
def test_wait_healthy_tolerates_a_slow_start(smoke, monkeypatch, ready_at):
    """Under CPU oversubscription `skinflint serve` took up to 46 s to answer its health
    check (measured on Windows); the wait must outlast that, and still give up."""
    clock = Clock()

    def urlopen(url, timeout):
        clock.now += 0.01
        if ready_at is None or clock.now < ready_at:
            raise ConnectionRefusedError
        return Healthy()

    monkeypatch.setattr(smoke, "time", clock)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    if ready_at is None:
        with pytest.raises(AssertionError, match="did not become healthy"):
            smoke.wait_healthy("http://127.0.0.1:1", Alive())
    else:
        smoke.wait_healthy("http://127.0.0.1:1", Alive())
        assert clock.now >= ready_at
