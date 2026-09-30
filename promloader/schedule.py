"""Выгрузка на Prom по расписанию (время компьютера) с учётом того, что компьютер мог быть выключен.

Если назначенное время прошло, пока программа не работала (нет света, выключен компьютер), поступаем
по настройке расписания:
- "ask"  — спросить при следующем открытии программы: «Выгрузить сейчас / Отложить на час / Пропустить»;
- "run"  — выгрузить сразу после запуска;
- "skip" — пропустить и ждать следующего раза.
Несколько пропущенных подряд запусков схлопываются в один — товары в любом случае отправляются в актуальном виде.
"""

import json
import re
from datetime import datetime, timedelta

from . import db, notify

GRACE = timedelta(minutes=10)      # опоздание до 10 минут — это ещё «вовремя»
SNOOZE = timedelta(hours=1)
MISSED_ACTIONS = {"ask": "спросить меня", "run": "выгрузить сразу", "skip": "пропустить"}
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SEND_CHUNK = 2000


class ScheduleError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse(row) -> dict:
    s = dict(row)
    s["days"] = json.loads(s["days"])
    s["enabled"] = bool(s["enabled"])
    s["next_run"] = next_slot(s["days"], s["time"], _now()).isoformat() if s["enabled"] else None
    s["label"] = label(s)
    return s


def label(s: dict) -> str:
    days = sorted(s["days"])
    if days == list(range(7)):
        when = "каждый день"
    elif days == list(range(5)):
        when = "по будням"
    else:
        when = ", ".join(DAY_NAMES[d] for d in days)
    return f"{when} в {s['time']}"


def _validate(data: dict) -> dict:
    out = {}
    if "days" in data:
        days = sorted({int(d) for d in data["days"]})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ScheduleError("Выберите хотя бы один день недели")
        out["days"] = json.dumps(days)
    if "time" in data:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(data["time"])):
            raise ScheduleError("Время в формате ЧЧ:ММ, например 09:00")
        out["time"] = data["time"]
    if "missed_action" in data:
        if data["missed_action"] not in MISSED_ACTIONS:
            raise ScheduleError("Неизвестное действие для пропущенной выгрузки")
        out["missed_action"] = data["missed_action"]
    if "enabled" in data:
        out["enabled"] = 1 if data["enabled"] else 0
    return out


def list_schedules() -> list[dict]:
    return [_parse(r) for r in db.query("SELECT * FROM schedules ORDER BY time, id")]


def get(schedule_id: int) -> dict:
    row = db.query_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
    if row is None:
        raise KeyError(schedule_id)
    return _parse(row)


def create(data: dict) -> dict:
    fields = _validate({"days": list(range(7)), "time": "09:00", "missed_action": "ask", **data})
    now = _now()
    # слоты до создания расписания не считаются пропущенными
    fields["last_slot"] = (last_slot(json.loads(fields["days"]), fields["time"], now) or now).isoformat()
    fields["created_at"] = now.isoformat()
    with db.tx() as c:
        cols = list(fields)
        cur = c.execute(f"INSERT INTO schedules ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        list(fields.values()))
    return get(cur.lastrowid)


def update(schedule_id: int, data: dict) -> dict:
    current = get(schedule_id)
    fields = _validate(data)
    if {"days", "time", "enabled"} & set(fields):
        days = json.loads(fields.get("days", json.dumps(current["days"])))
        slot = last_slot(days, fields.get("time", current["time"]), _now())
        fields["last_slot"] = slot.isoformat() if slot else _now().isoformat()
        fields["pending_slot"] = None
    if fields:
        with db.tx() as c:
            c.execute(f"UPDATE schedules SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      list(fields.values()) + [schedule_id])
    return get(schedule_id)


def delete(schedule_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# ---------- время ----------

def _slot_on(day: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def last_slot(days: list[int], hhmm: str, now: datetime) -> datetime | None:
    """Последнее назначенное время, которое уже наступило (за последнюю неделю)."""
    for back in range(8):
        slot = _slot_on(now - timedelta(days=back), hhmm)
        if slot.weekday() in days and slot <= now:
            return slot
    return None


def next_slot(days: list[int], hhmm: str, now: datetime) -> datetime:
    for ahead in range(8):
        slot = _slot_on(now + timedelta(days=ahead), hhmm)
        if slot.weekday() in days and slot > now:
            return slot
    return now


# ---------- выполнение ----------

def _send_ready() -> str:
    from . import sync

    ids = [r["id"] for r in db.query("SELECT id FROM products WHERE status = 'ready' ORDER BY id")]
    if not ids:
        return "Нечего отправлять: нет товаров «готов к отправке»"
    accepted = rejected = 0
    for start in range(0, len(ids), SEND_CHUNK):
        res = sync.enqueue(ids[start:start + SEND_CHUNK])
        accepted += res["accepted"]
        rejected += len(res["rejected"])
    text = f"Поставлено в отправку: {accepted}"
    return text + (f", не прошли проверку: {rejected}" if rejected else "")


def _save(schedule_id: int, **fields) -> None:
    with db.tx() as c:
        c.execute(f"UPDATE schedules SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  list(fields.values()) + [schedule_id])


def _run(s: dict, slot: datetime | None, prefix: str = "") -> None:
    now = _now()
    result = _send_ready()
    fields = {"last_run_at": now.isoformat(), "last_result": prefix + result, "pending_slot": None, "snooze_until": None}
    if slot is not None:
        fields["last_slot"] = slot.isoformat()
    _save(s["id"], **fields)


def tick(now: datetime | None = None) -> list[str]:
    """Проверка расписаний. Вызывается фоновой задачей каждые ~20 секунд. Возвращает, что сделано (для журнала)."""
    now = now or _now()
    done = []
    for row in db.query("SELECT * FROM schedules WHERE enabled = 1"):
        s = _parse(row)
        snooze = _dt(s["snooze_until"])
        if snooze and now >= snooze:
            _run(s, None, "Отложенная выгрузка. ")
            done.append(f"{s['label']}: отложенная выгрузка")
            continue
        slot = last_slot(s["days"], s["time"], now)
        if slot is None or (s["last_slot"] and slot <= _dt(s["last_slot"])):
            continue
        late = now - slot
        when = slot.strftime("%d.%m %H:%M")
        if late <= GRACE:
            _run(s, slot)
            done.append(f"{s['label']}: выгрузка по расписанию")
        elif s["missed_action"] == "run":
            _run(s, slot, f"Выгрузка за {when} была пропущена (программа не работала) — выполнена после запуска. ")
            done.append(f"{s['label']}: пропущенная выгрузка выполнена")
        elif s["missed_action"] == "skip":
            _save(s["id"], last_slot=slot.isoformat(),
                  last_result=f"Выгрузка за {when} пропущена: программа не работала в это время")
            done.append(f"{s['label']}: пропущено")
        else:
            _save(s["id"], last_slot=slot.isoformat(), pending_slot=slot.isoformat())
            done.append(f"{s['label']}: ждёт решения")
            notify.send("schedule_missed", f"⏰ Выгрузка на Prom за {when} ({s['label']}) не выполнена — программа не работала. "
                                           "Откройте Prom Loader и выберите: выгрузить сейчас, отложить или пропустить.")
    return done


def missed() -> list[dict]:
    return [{"id": s["id"], "label": s["label"], "slot": s["pending_slot"]}
            for s in list_schedules() if s["pending_slot"] and s["enabled"]]


def resolve(schedule_id: int, decision: str) -> dict:
    s = get(schedule_id)
    if not s["pending_slot"]:
        raise ScheduleError("Для этого расписания нет пропущенной выгрузки")
    when = _dt(s["pending_slot"]).strftime("%d.%m %H:%M")
    if decision == "run":
        _run(s, None, f"Пропущенная выгрузка за {when} выполнена по вашему решению. ")
    elif decision == "snooze":
        until = _now() + SNOOZE
        _save(s["id"], pending_slot=None, snooze_until=until.isoformat(),
              last_result=f"Пропущенная выгрузка за {when} отложена до {until.strftime('%H:%M')}")
    elif decision == "skip":
        _save(s["id"], pending_slot=None, last_result=f"Пропущенная выгрузка за {when} отменена")
    else:
        raise ScheduleError("Неизвестное решение")
    return get(schedule_id)
