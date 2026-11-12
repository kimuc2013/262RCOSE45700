"""단계별 스크립트가 함께 쓰는 경로, 설정, HTTP 요청."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]          # backfill/
load_dotenv(ROOT / ".env")

DATA = ROOT / os.getenv("DATA_DIR", "data")
RAW_HTML = DATA / "raw" / "html"
CRAWL_STATE = DATA / "raw" / "crawl_state.json"   # 내부 수집 기록. 넘겨줄 때 붙이는 manifest.json과 다름
REPORTS = DATA / "reports"

KST = timezone(timedelta(hours=9))


def require_env(*names: str) -> dict[str, str]:
    """필수 환경변수를 읽는다. 하나라도 비어 있으면 빠진 이름을 알려주고 멈춘다."""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise SystemExit(f".env에 {', '.join(missing)} 값이 없습니다. backfill/README.md의 변수 목록을 참고하세요.")
    return {n: os.environ[n] for n in names}


def semester_key(semester: str) -> tuple[int, int]:
    """'2025-1' → (2025, 1). 학기 비교용."""
    year, term = semester.split("-")
    return int(year), int(term)


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def load_sources() -> dict:
    with open(ROOT / "config" / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Fetcher:
    """요청 사이 간격을 지키는 HTTP 클라이언트."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = os.getenv(
            "USER_AGENT", "KU-AcademicInfo-MCP-Backfill/0.1 (student project)")
        self.interval = float(os.getenv("REQUEST_INTERVAL_SEC", "1.0"))
        self._last = 0.0

    def get(self, url: str, **kwargs) -> requests.Response:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(url, timeout=20, **kwargs)
        finally:
            self._last = time.monotonic()
