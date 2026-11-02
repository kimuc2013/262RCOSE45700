"""학사일정 표 → data/calendar.jsonl (기획안 18p 형식, 한 줄에 일정 하나).

crawl.py가 저장한 학기별 학사일정 HTML을 읽는다.
표에는 연도 없이 '월' 칸과 '11(수)~13(금)' 같은 날짜만 있으므로 연도를 추정하고,
함께 적힌 요일로 추정이 맞는지 검증한다. 요일이 하나라도 어긋나면 실패로 끝낸다.

    python src/parse_calendar.py
"""
import re
import sys
from datetime import date

from bs4 import BeautifulSoup

from collections import Counter

from common import (CRAWL_STATE, DATA, RAW_HTML, REPORTS, now_iso, read_json, today, write_json,
                    write_jsonl)

WEEKDAYS = "월화수목금토일"
# '11(수)', '3.2(월)' (월이 바뀌는 경우 대비) 형태의 날짜 하나
DAY = re.compile(r"(?:(\d{1,2})\.)?(\d{1,2})\(([월화수목금토일])\)")


# 행사 이름 속 학기 표기: '2027학년도 제1학기 수강신청', '제2학기 기말고사'
TERM_IN_NAME = re.compile(r"(?:(\d{4})학년도\s*)?제\s*([12])\s*학기")


def target_semester(name: str, semester: str) -> str:
    """행사가 해당하는 학기. 이름에 학기가 없으면 실린 학기 표와 같다고 본다."""
    m = TERM_IN_NAME.search(name)
    if not m:
        return semester
    year, term = int(semester.split("-")[0]), int(semester.split("-")[1])
    t = int(m.group(2))
    if m.group(1):
        return f"{m.group(1)}-{t}"
    # 연도 없이 '제1학기'가 2학기 표에 나오면 다음 해 1학기
    return f"{year + 1 if t < term else year}-{t}"


class WeekdayMismatch(ValueError):
    pass


def make_date(y: int, m: int, d: int, wd: str) -> date:
    dt = date(y, m, d)
    if WEEKDAYS[dt.weekday()] != wd:
        raise WeekdayMismatch(f"{dt} 은(는) {WEEKDAYS[dt.weekday()]}요일인데 표에는 ({wd})")
    return dt


def parse_dates(text: str, year: int, month: int) -> tuple[date, date]:
    days = DAY.findall(text)
    if not days or len(days) > 2:
        raise ValueError(f"날짜 형식을 읽을 수 없음: {text!r}")

    sm, sd, swd = days[0]
    sm = int(sm) if sm else month
    start = make_date(year, sm, int(sd), swd)
    if len(days) == 1:
        return start, start

    em, ed, ewd = days[1]
    if em:
        em = int(em)
    else:
        em = sm if int(ed) >= int(sd) else sm % 12 + 1      # 끝 날짜가 더 작으면 다음 달
    ey = year + 1 if em < sm else year
    return start, make_date(ey, em, int(ed), ewd)


def parse_table(html: bytes, semester: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml", from_encoding="utf-8")
    table = soup.select_one(".college_schedule table")
    if table is None:
        raise ValueError("학사일정 표를 찾지 못함")

    base_year = int(semester.split("-")[0])
    first_month = None
    month = None
    rows = []
    for tr in table.find_all("tr"):
        th = tr.find("th")
        if th:
            month = int(re.search(r"\d+", th.get_text()).group())
            first_month = first_month or month
        date_td = tr.select_one("td.dateInfo")
        name_td = tr.select("td.des")[-1] if tr.select("td.des") else None
        if month is None or date_td is None or name_td is None:
            continue
        # 2학기 표는 8월에서 다음 해 2월까지 이어짐: 첫 달보다 작은 달은 다음 해
        year = base_year + 1 if month < first_month else base_year
        date_text = " ".join(date_td.get_text().split())
        start, end = parse_dates(date_text, year, month)
        rows.append({
            "name": " ".join(name_td.get_text().split()),
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
    return rows


def main():
    state = read_json(CRAWL_STATE, {})
    cal_docs = {k: v for k, v in state.items() if v.get("kind") == "calendar" and v.get("ok")}
    if not cal_docs:
        print("학사일정 원문이 없음. 먼저 python src/crawl.py --only calendar 실행")
        return 1

    events, summary, errors = [], {}, {}
    for doc_id, meta in sorted(cal_docs.items()):
        semester = meta["semester"]
        try:
            rows = parse_table((RAW_HTML / meta["file"]).read_bytes(), semester)
        except ValueError as e:
            errors[semester] = str(e)
            print(f"[ERR] {semester}: {e}")
            continue
        # event_id = calendar:{학기}:{행사명(공백 제거)}-{순번}. 순번은 같은 표에서 같은 이름끼리만 셈
        # → 일정 날짜가 바뀌어도 ID는 유지됨
        seen = Counter()
        for r in rows:
            key = re.sub(r"\s+", "", r["name"])
            seen[key] += 1
            events.append({
                "event_id": f"calendar:{semester}:{key}-{seen[key]}",
                "doc_title": r["name"],
                "start": r["start"],
                "end": r["end"],
                "semester": semester,
                "target_semester": target_semester(r["name"], semester),
                "scope": meta["scope"],
                "url": meta["url"],
                "collected_at": meta["fetched_at"][:10],
            })
        summary[semester] = len(rows)
        print(f"[OK ] {semester}: {len(rows)}건  ({rows[0]['start']} ~ {rows[-1]['end']})")

    if len({e["event_id"] for e in events}) != len(events):
        print("[ERR] event_id 중복")
        return 1

    write_jsonl(DATA / "calendar.jsonl", events)
    write_json(REPORTS / f"calendar_{today()}.json",
               {"run_at": now_iso(), "rows_per_semester": summary, "errors": errors, "total": len(events)})
    print(f"\ncalendar.jsonl: {len(events)}건, 오류 {len(errors)}학기")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
