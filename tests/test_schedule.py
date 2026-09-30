from datetime import datetime, timedelta

import pytest

from promloader import db, products, schedule, sync

MON_0800 = datetime(2026, 9, 28, 8, 0)  # понедельник


@pytest.fixture
def clock(monkeypatch):
    state = {"now": MON_0800}
    monkeypatch.setattr(schedule, "_now", lambda: state["now"])
    return state


def ready(n=2):
    ids = [products.create({"name": f"Товар {i}", "price": 10}) for i in range(n)]
    products.set_status(ids, "ready")
    return ids


def queued():
    return sum(j["count"] for j in sync.list_jobs())


def test_runs_on_time_once(clock):
    ready()
    s = schedule.create({"time": "09:00"})
    assert s["label"] == "каждый день в 09:00"
    assert s["next_run"] == "2026-09-28T09:00:00"
    assert schedule.tick(datetime(2026, 9, 28, 8, 59)) == []
    assert schedule.tick(datetime(2026, 9, 28, 9, 3)) == ["каждый день в 09:00: выгрузка по расписанию"]
    assert queued() == 2
    assert schedule.tick(datetime(2026, 9, 28, 9, 4)) == []  # второй раз не выгружает
    assert "Поставлено в отправку: 2" in schedule.get(s["id"])["last_result"]


def test_missed_ask_then_run(clock):
    ready()
    s = schedule.create({"time": "09:00", "missed_action": "ask"})
    # компьютер был выключен с понедельника до вторника 12:00 — два пропуска схлопываются в один вопрос
    clock["now"] = datetime(2026, 9, 29, 12, 0)
    assert schedule.tick(clock["now"]) == ["каждый день в 09:00: ждёт решения"]
    assert queued() == 0
    missed = schedule.missed()
    assert missed == [{"id": s["id"], "label": "каждый день в 09:00", "slot": "2026-09-29T09:00:00"}]
    assert schedule.tick(clock["now"] + timedelta(minutes=1)) == []  # не спрашивает повторно
    schedule.resolve(s["id"], "run")
    assert queued() == 2 and schedule.missed() == []
    assert "выполнена по вашему решению" in schedule.get(s["id"])["last_result"]


def test_missed_snooze(clock):
    ready()
    s = schedule.create({"time": "09:00"})
    clock["now"] = datetime(2026, 9, 28, 15, 0)
    schedule.tick(clock["now"])
    schedule.resolve(s["id"], "snooze")
    assert "отложена до 16:00" in schedule.get(s["id"])["last_result"]
    assert schedule.tick(datetime(2026, 9, 28, 15, 30)) == []
    assert schedule.tick(datetime(2026, 9, 28, 16, 0)) == ["каждый день в 09:00: отложенная выгрузка"]
    assert queued() == 2


def test_missed_run_immediately(clock):
    ready()
    schedule.create({"time": "09:00", "missed_action": "run"})
    assert schedule.tick(datetime(2026, 9, 28, 18, 0)) == ["каждый день в 09:00: пропущенная выгрузка выполнена"]
    assert queued() == 2


def test_missed_skip(clock):
    ready()
    s = schedule.create({"time": "09:00", "missed_action": "skip"})
    assert schedule.tick(datetime(2026, 9, 28, 18, 0)) == ["каждый день в 09:00: пропущено"]
    assert queued() == 0 and "пропущена" in schedule.get(s["id"])["last_result"]
    with pytest.raises(schedule.ScheduleError):
        schedule.resolve(s["id"], "run")


def test_days_and_nothing_to_send(clock):
    s = schedule.create({"time": "10:00", "days": [5, 6]})  # только выходные
    assert s["label"] == "Сб, Вс в 10:00" and s["next_run"] == "2026-10-03T10:00:00"
    assert schedule.tick(datetime(2026, 9, 29, 10, 1)) == []  # вторник
    schedule.tick(datetime(2026, 10, 3, 10, 1))
    assert "Нечего отправлять" in schedule.get(s["id"])["last_result"]


def test_slots_before_creation_are_not_missed(clock):
    clock["now"] = datetime(2026, 9, 28, 20, 0)
    schedule.create({"time": "09:00"})
    assert schedule.tick(clock["now"]) == []


def test_disabled_and_validation(clock):
    s = schedule.create({"time": "09:00", "enabled": False})
    assert s["next_run"] is None
    assert schedule.tick(datetime(2026, 9, 28, 9, 1)) == []
    for bad in ({"time": "25:00"}, {"days": []}, {"missed_action": "maybe"}):
        with pytest.raises(schedule.ScheduleError):
            schedule.update(s["id"], bad)


def test_api(client, clock):
    s = client.post("/api/schedules", json={"time": "07:30", "days": [0, 1, 2, 3, 4]}).json()
    assert s["label"] == "по будням в 07:30"
    data = client.get("/api/schedules").json()
    assert data["autostart"]["supported"] is False and len(data["items"]) == 1
    assert client.patch(f"/api/schedules/{s['id']}", json={"time": "7:30"}).status_code == 400
    clock["now"] = datetime(2026, 9, 29, 12, 0)  # вторник: утренняя выгрузка пропущена
    schedule.tick(clock["now"])
    assert client.get("/api/meta").json()["missed_schedules"][0]["id"] == s["id"]
    assert client.post(f"/api/schedules/{s['id']}/resolve", json={"decision": "skip"}).status_code == 200
    assert client.post("/api/autostart", json={"enabled": True}).status_code == 400
    assert client.delete(f"/api/schedules/{s['id']}").json() == {"ok": True}
