"""Отчёты."""


from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import reports

router = APIRouter()


def _report(tab: str, period: str, date_from: str, date_to: str, dead_days: int) -> dict:
    try:
        return reports.build(tab, period, date_from, date_to, max(1, min(int(dead_days), 3650)))
    except reports.ReportError as exc:
        raise HTTPException(400, str(exc))


# .xlsx — раньше общего маршрута: иначе «sales.xlsx» попал бы в {tab}
@router.get("/api/reports/{tab}.xlsx")
def report_xlsx(tab: str, period: str = "30d", date_from: str = "", date_to: str = "", dead_days: int = 30):
    report = _report(tab, period, date_from, date_to, dead_days)
    name = f"otchet-{tab}-{report['from']}-{report['to']}.xlsx"
    return Response(reports.xlsx(report), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/reports/{tab}")
def report(tab: str, period: str = "30d", date_from: str = "", date_to: str = "", dead_days: int = 30):
    return _report(tab, period, date_from, date_to, dead_days)
