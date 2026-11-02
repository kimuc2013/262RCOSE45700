"""1단계 크롤링: sources.yaml의 공개 페이지를 받아 data/raw/html/에 원문 그대로 저장.

    python src/crawl.py                # 학사일정 + 안내 페이지 전체
    python src/crawl.py --only calendar  # 학사일정만 (매주 재수집)
    python src/crawl.py --only pages     # 안내 페이지만 (학기마다 재수집)

결과
- data/raw/html/{doc_id}.html          원문 HTML
- data/raw/crawl_state.json         문서별 메타 (URL, 수집 시각, 해시, 첨부 링크 등)
- data/reports/crawl_{날짜}.json        수집 건수, 실패, 로그인 리다이렉트, 빈 본문 (기획안 14p)
"""
import argparse
import hashlib
import sys
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from common import (CRAWL_STATE, RAW_HTML, REPORTS, Fetcher, load_sources,
                    now_iso, read_json, today, write_json)

MIN_BODY_CHARS = 50          # 본문이 이보다 짧으면 빈 본문으로 보고 리포트에 올림
ATTACH_EXT = (".pdf", ".hwp", ".hwpx", ".xls", ".xlsx", ".doc", ".docx", ".zip")


def doc_title(soup: BeautifulSoup, category: str) -> str:
    # 안내 페이지: <title>고려대학교 교육정보 | 교육정보 | 전공 | 제2전공 | 이중전공</title>
    parts = [p.strip() for p in soup.title.get_text().split("|")] if soup.title else []
    if len(parts) > 1 and parts[0] == "고려대학교 교육정보":
        return " > ".join(parts[1:])
    # 게시판형 페이지는 <title>에 메뉴 경로가 없음 → 분류 > 페이지 제목(h3)
    h3 = soup.select_one("section.content .title h3")
    return f"{category} > {h3.get_text(strip=True)}" if h3 else category


def find_attachments(main, page_url: str) -> list[dict]:
    found = {}
    for a in main.find_all("a", href=True):
        href = a["href"]
        if href.lower().split("?")[0].endswith(ATTACH_EXT) or "mode=download" in href:
            url = urljoin(page_url, href)
            found.setdefault(url, {"url": url, "name": a.get_text(" ", strip=True) or None})
    return list(found.values())


def fetch_page(fetcher: Fetcher, url: str, file_stem: str, category: str, old: dict | None) -> dict:
    meta = {"url": url, "fetched_at": now_iso()}
    try:
        res = fetcher.get(url)
    except Exception as e:
        return meta | {"ok": False, "error": repr(e)}

    meta |= {"status": res.status_code, "final_url": res.url}
    if "login" in urlparse(res.url).path.lower():
        return meta | {"ok": False, "login_redirect": True}
    if res.status_code != 200:
        return meta | {"ok": False}

    res.encoding = "utf-8"
    soup = BeautifulSoup(res.text, "lxml")
    main = soup.select_one("#jwxe_main_content")
    body_chars = len(main.get_text(strip=True)) if main else 0
    sha = hashlib.sha256(res.content).hexdigest()

    path = RAW_HTML / f"{file_stem}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(res.content)

    # 본문이 외부 시스템 iframe뿐인 페이지 (개설과목, 졸업학점 이수기준표 등)
    iframes = [urljoin(url, f["src"]) for f in main.find_all("iframe", src=True)] if main else []

    return meta | {
        "ok": True,
        "file": path.name,
        "doc_title": doc_title(soup, category),
        "body_chars": body_chars,
        "empty_body": body_chars < MIN_BODY_CHARS,
        "iframes": iframes,
        "attachments": find_attachments(main, url) if main else [],
        "raw_sha256": sha,
        "raw_changed": old is None or old.get("raw_sha256") != sha,
    }


def calendar_targets(fetcher: Fetcher, base: str, cal: dict) -> list[tuple[str, str]]:
    """학기 선택 목록을 읽어 (semester, url) 목록을 만든다. 예: ("2026-2", ".../schedule.do?cYear=2026&hakGi=2&srCategoryId1=1668")"""
    url = base + cal["path"]
    soup = BeautifulSoup(fetcher.get(url).content, "lxml", from_encoding="utf-8")
    targets = []
    for opt in soup.select("select#sCateYear option"):
        year, term = opt["data-year"], opt["data-term"]
        if cal.get("semesters", "all") == "current" and not opt.has_attr("selected"):
            continue
        targets.append((f"{year}-{term}", f"{url}?cYear={year}&hakGi={term}&srCategoryId1={opt['value']}"))
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["calendar", "pages"])
    args = ap.parse_args()

    src = load_sources()
    base = src["site"]["base_url"]
    defaults = src.get("defaults", {})
    fetcher = Fetcher()

    robots = RobotFileParser(base + "/robots.txt")
    robots.read()
    ua = fetcher.session.headers["User-Agent"]

    state = read_json(CRAWL_STATE, {})
    results = []

    def run(doc_id: str, url: str, file_stem: str, extra: dict, override: dict | None = None):
        if not robots.can_fetch(ua, url):
            r = {"url": url, "ok": False, "robots_disallowed": True}
        else:
            r = fetch_page(fetcher, url, file_stem, extra["category"], state.get(doc_id))
        r = extra | r | (override or {})
        state[doc_id] = r
        results.append((doc_id, r))
        flag = "OK " if r["ok"] else "ERR"
        print(f"[{flag}] {doc_id:28s} {r.get('body_chars', '-'):>6} chars  {r.get('doc_title', r.get('error', ''))}")

    if args.only in (None, "calendar"):
        cal = src["calendar"]
        for semester, url in calendar_targets(fetcher, base, cal):
            doc_id = f"{cal['doc_id']}_{semester}"
            year, term = semester.split("-")
            run(doc_id, url, doc_id, {"kind": "calendar", "semester": semester, "category": cal["category"],
                                      "scope": cal.get("scope", defaults.get("scope")),
                                      "recrawl": cal.get("recrawl", defaults.get("recrawl"))},
                override={"doc_title": f"{year}학년도 {term}학기 학사일정"})

    if args.only in (None, "pages"):
        for p in src["pages"]:
            if not p.get("enabled", True):
                continue
            run(p["doc_id"], base + p["path"], p["doc_id"],
                {"kind": "page", "category": p["category"],
                 "scope": p.get("scope", defaults.get("scope")),
                 "recrawl": p.get("recrawl", defaults.get("recrawl"))})

    write_json(CRAWL_STATE, state)

    ok = [d for d, r in results if r["ok"]]
    report = {
        "run_at": now_iso(),
        "only": args.only,
        "requested": len(results),
        "ok": len(ok),
        "failed": [d for d, r in results if not r["ok"]],
        "login_redirect": [d for d, r in results if r.get("login_redirect")],
        "empty_body": [d for d, r in results if r.get("empty_body") and not r.get("iframes")],
        "iframe_only": {d: r["iframes"] for d, r in results if r.get("empty_body") and r.get("iframes")},
        "empty_body_ratio": round(sum(1 for _, r in results if r.get("empty_body") and not r.get("iframes"))
                                  / max(len(ok), 1), 3),
        "raw_changed": [d for d, r in results if r.get("raw_changed")],
        "attachments": sum(len(r.get("attachments", [])) for _, r in results),
    }
    write_json(REPORTS / f"crawl_{today()}.json", report)
    print(f"\n수집 {report['ok']}/{report['requested']}, 실패 {len(report['failed'])}, "
          f"빈 본문 {len(report['empty_body'])}, 외부 iframe {len(report['iframe_only'])}, 원문 변경 {len(report['raw_changed'])}, 첨부 링크 {report['attachments']}")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
