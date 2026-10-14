from types import SimpleNamespace

from common.progress_utils import _load_fresh_task
from common.task_models import TaskStatus


class _Result:
    def __init__(self, task):
        self.task = task

    def first(self):
        return self.task


class _Session:
    def __init__(self):
        self.expired = False
        self.task = SimpleNamespace(
            task_id="task-1",
            game_builder_status=TaskStatus.PENDING,
        )

    def expire_all(self):
        self.expired = True
        self.task.game_builder_status = TaskStatus.COMPLETED

    def exec(self, _statement):
        assert self.expired is True
        return _Result(self.task)


def test_load_fresh_task_expires_cached_stage_state():
    session = _Session()

    task = _load_fresh_task(session, "task-1")

    assert task.game_builder_status == TaskStatus.COMPLETED
