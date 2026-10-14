import json
import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

from common import cg_data


def _engine_with_cg(payload=None, updated_at=None):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE cg (task_id TEXT, data TEXT, update_time TEXT)")
        )
        if payload is not None:
            connection.execute(
                text(
                    "INSERT INTO cg (task_id, data, update_time) "
                    "VALUES ('task-1', :data, :updated_at)"
                ),
                {
                    "data": json.dumps(payload),
                    "updated_at": (updated_at or datetime.now()).isoformat(),
                },
            )
    return engine


def _engine_with_legacy_cg(payload=None):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE cg (task_id TEXT, data TEXT)"))
        if payload is not None:
            connection.execute(
                text("INSERT INTO cg (task_id, data) VALUES ('task-1', :data)"),
                {"data": json.dumps(payload)},
            )
    return engine


def test_cg_contract_falls_back_to_shared_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cg_data, "local_artifact_roots", lambda: [tmp_path])
    cg_data.write_local_cg_contract(
        "task-1", {"cg_1": {"cg_prompt": "local"}}, outputs_root=tmp_path
    )

    assert cg_data.load_cg_contract(
        "task-1", db_engine=_engine_with_cg()
    )["cg_1"]["cg_prompt"] == "local"


def test_newer_shared_cg_contract_overrides_stale_database(monkeypatch, tmp_path):
    monkeypatch.setattr(cg_data, "local_artifact_roots", lambda: [tmp_path])
    old = datetime.now() - timedelta(hours=1)
    engine = _engine_with_cg({"cg_1": {"cg_prompt": "database"}}, old)
    path = cg_data.write_local_cg_contract(
        "task-1", {"cg_1": {"cg_prompt": "fallback"}}, outputs_root=tmp_path
    )
    os.utime(path, None)

    assert cg_data.load_cg_contract("task-1", db_engine=engine)["cg_1"][
        "cg_prompt"
    ] == "fallback"


def test_newer_database_remains_authoritative(monkeypatch, tmp_path):
    monkeypatch.setattr(cg_data, "local_artifact_roots", lambda: [tmp_path])
    path = cg_data.write_local_cg_contract(
        "task-1", {"cg_1": {"cg_prompt": "old-local"}}, outputs_root=tmp_path
    )
    old_timestamp = (datetime.now() - timedelta(hours=2)).timestamp()
    os.utime(path, (old_timestamp, old_timestamp))
    engine = _engine_with_cg(
        {"cg_1": {"cg_prompt": "new-database"}}, datetime.now()
    )

    assert cg_data.load_cg_contract("task-1", db_engine=engine)["cg_1"][
        "cg_prompt"
    ] == "new-database"


def test_legacy_cg_table_without_update_time_still_loads_database(monkeypatch, tmp_path):
    monkeypatch.setattr(cg_data, "local_artifact_roots", lambda: [tmp_path])
    engine = _engine_with_legacy_cg({"cg_1": {"cg_prompt": "legacy-database"}})

    assert cg_data.load_cg_contract("task-1", db_engine=engine)["cg_1"][
        "cg_prompt"
    ] == "legacy-database"


def test_naive_database_timestamp_is_interpreted_in_application_timezone():
    naive = datetime(2026, 8, 25, 18, 0, 0)

    assert cg_data._datetime_timestamp(naive) == naive.replace(
        tzinfo=cg_data.APP_TZ
    ).timestamp()
