import threading
import time

from common.image_concurrency import run_dependency_layer


def test_dependency_layer_preserves_order_while_running_concurrently():
    active = 0
    peak = 0
    guard = threading.Lock()

    def worker(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return value * 2

    assert run_dependency_layer(
        [1, 2, 3, 4],
        worker,
        concurrency=4,
        stage="test",
        task_id="task",
    ) == [2, 4, 6, 8]
    assert peak > 1


def test_global_provider_slot_remains_opt_in(monkeypatch):
    monkeypatch.delenv("IMAGE_PROVIDER_GLOBAL_CONCURRENCY", raising=False)

    assert run_dependency_layer(
        [1],
        lambda value: value,
        concurrency=1,
        stage="test",
        task_id="task",
    ) == [1]
