"""2단계 첨부 받기: 크롤링한 페이지에 걸린 첨부(PDF, HWP 등) 원본을 data/raw/attachments/에 보관.

    python src/fetch_attachments.py

- 같은 파일이 여러 페이지에 걸려 있으면 한 번만 받는다 (URL 기준).
- 이미 받은 파일은 조건부 요청(If-None-Match / If-Modified-Since)으로 확인하고,
  서버가 304(변경 없음)를 주면 다시 받지 않는다. 헤더가 없는 서버면 받은 뒤 SHA-256으로 비교한다.
- sources.yaml의 attachments.exclude_url_contains에 걸리는 첨부는 받지 않고 제외 사유만 남긴다.
- scope는 sources.yaml의 attachments.scope(파일명 기준)를 따르고, 없으면 처음 걸린 페이지의 scope를 쓴다.

결과
- data/raw/attachments/{파일명}           원본
- data/raw/attachments.json              첨부별 메타 (어느 페이지에 걸렸는지, 해시, 형식 등)
- data/reports/attachments_{날짜}.json    받은 수, 변경 없음, 실패, 제외
"""
import hashlib
import sys
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from common import (CRAWL_STATE, DATA, REPORTS, Fetcher, load_sources, now_iso, read_json,
                    today, write_json)

ATTACH_DIR = DATA / "raw" / "attachments"
ATTACH_STATE = DATA / "raw" / "attachments.json"

# 파일 앞부분으로 실제 형식 판별 (확장자나 Content-Type이 틀린 경우 대비)
MAGIC = [
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "hwp"),   # OLE 복합 문서 (HWP 5.0, 옛 doc/xls도 같은 형식)
    (b"PK\x03\x04", "zip"),                          # HWPX, docx, xlsx 등
]


def detect_type(head: bytes, filename: str) -> str:
    for magic, kind in MAGIC:
        if head.startswith(magic):
            if kind == "zip":
                ext = PurePosixPath(filename).suffix.lower().lstrip(".")
                return ext if ext in ("hwpx", "docx", "xlsx", "pptx") else "zip"
            if kind == "hwp" and not filename.lower().endswith(".hwp"):
                return PurePosixPath(filename).suffix.lower().lstrip(".") or "ole"
            return kind
    return "unknown"


def collect_links(state: dict) -> dict[str, dict]:
    """crawl_state의 페이지별 첨부 링크를 URL 기준으로 모은다. pages는 처음 발견한 순서."""
    links = {}
    for doc_id, meta in state.items():
        for a in meta.get("attachments", []):
            item = links.setdefault(a["url"], {"url": a["url"], "name": a.get("name"), "pages": []})
            item["pages"].append(doc_id)
    return links


def file_name_for(url: str) -> str:
    name = unquote(PurePosixPath(urlparse(url).path).name)
    return name or hashlib.sha256(url.encode()).hexdigest()[:16]


def main():
    src = load_sources()
    excludes = src.get("attachments", {}).get("exclude_url_contains", [])
    scope_map = src.get("attachments", {}).get("scope", {})
    crawl_state = read_json(CRAWL_STATE, {})
    links = collect_links(crawl_state)
    old_state = read_json(ATTACH_STATE, {})
    fetcher = Fetcher()
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)

    new_state, results = {}, {"downloaded": [], "not_modified": [], "unchanged_after_download": [],
                               "failed": [], "excluded": []}
    for url, link in links.items():
        old = old_state.get(url, {})
        scope = scope_map.get(file_name_for(url)) or crawl_state[link["pages"][0]].get("scope")
        entry = {"url": url, "name": link["name"], "pages": link["pages"], "scope": scope}

        rule = next((r for r in excludes if r in url), None)
        if rule:
            new_state[url] = entry | {"excluded": f"exclude_url_contains: {rule}"}
            results["excluded"].append(url)
            continue

        headers = {}
        if old.get("file") and (ATTACH_DIR / old["file"]).exists():
            if old.get("etag"):
                headers["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                headers["If-Modified-Since"] = old["last_modified"]

        try:
            res = fetcher.get(url, headers=headers)
        except Exception as e:
            new_state[url] = old | entry | {"error": repr(e), "checked_at": now_iso()}
            results["failed"].append(url)
            print(f"[ERR] {url}  {e!r}")
            continue

        if res.status_code == 304:
            new_state[url] = old | entry | {"checked_at": now_iso(), "changed": False}
            results["not_modified"].append(url)
            print(f"[304] {old['file']}")
            continue
        if res.status_code != 200:
            new_state[url] = old | entry | {"error": f"HTTP {res.status_code}", "checked_at": now_iso()}
            results["failed"].append(url)
            print(f"[ERR] {url}  HTTP {res.status_code}")
            continue

        body = res.content
        sha = hashlib.sha256(body).hexdigest()
        fname = file_name_for(url)
        changed = old.get("sha256") != sha
        if changed:
            (ATTACH_DIR / fname).write_bytes(body)
        new_state[url] = entry | {
            "file": fname,
            "file_type": detect_type(body[:8], fname),
            "content_type": res.headers.get("Content-Type"),
            "bytes": len(body),
            "sha256": sha,
            "etag": res.headers.get("ETag"),
            "last_modified": res.headers.get("Last-Modified"),
            "fetched_at": now_iso() if changed else old.get("fetched_at"),
            "checked_at": now_iso(),
            "changed": changed,
        }
        results["downloaded" if changed else "unchanged_after_download"].append(url)
        print(f"[{'NEW' if changed else 'SAME'}] {fname:45s} {new_state[url]['file_type']:5s} {len(body):>9,} B  "
              f"{link['name']}  ← {', '.join(link['pages'][:3])}{' …' if len(link['pages']) > 3 else ''}")

    # 페이지에서 사라진 첨부: 원본은 남기고 기록만 표시
    for url, old in old_state.items():
        if url not in new_state:
            new_state[url] = old | {"removed_from_pages": True, "checked_at": now_iso()}

    write_json(ATTACH_STATE, new_state)
    report = {"run_at": now_iso(), "unique_links": len(links)} | {k: len(v) for k, v in results.items()} \
        | {"failed_urls": results["failed"], "excluded_urls": results["excluded"],
           "file_types": {t: sum(1 for e in new_state.values() if e.get("file_type") == t)
                          for t in sorted({e.get("file_type") for e in new_state.values() if e.get("file_type")})}}
    write_json(REPORTS / f"attachments_{today()}.json", report)
    print(f"\n첨부 {len(links)}개: 새로 받음 {len(results['downloaded'])}, 변경 없음(304) {len(results['not_modified'])}, "
          f"받았지만 같음 {len(results['unchanged_after_download'])}, 실패 {len(results['failed'])}, "
          f"제외 {len(results['excluded'])}  형식 {report['file_types']}")
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
