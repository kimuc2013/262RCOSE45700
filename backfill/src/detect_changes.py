"""5단계 중복 감지·변경 감지 (기획안 19p): 문서마다 64비트 SimHash 지문을 만들어 지난 실행과 비교.

    python src/detect_changes.py            # 비교 후 지문 기록 갱신
    python src/detect_changes.py --dry-run  # 비교만 하고 기록은 그대로

입력: data/clean/{doc_id}.json, data/calendar.jsonl
결과
- data/export/changes.json            색인 대상 문서·학사일정 행사의 추가/변경/삭제 목록 (서빙: 캐시 무효화, 모델: 재임베딩·인덱스 갱신)
- data/raw/snapshots/latest.json      비교 기준이 되는 지문 기록. 실행마다 날짜별 사본도 남김
- data/reports/changes_{날짜}.json     문서별 거리, 재임베딩 절감률, 중복 감지 결과

판정 (문서)
- SHA-256이 같으면 그대로
- SHA-256은 다르지만 SimHash 해밍 거리가 기준값(sources.yaml change_detection.simhash_threshold) 이하면 건너뜀
  (실질적 변경이 아님 → 재처리·재임베딩 안 함, 캐시 유지). 건너뛴 문서는 skipped에 남기고, 지문 기준은 갱신하지 않음
  → 작은 변경이 쌓여 기준값을 넘으면 그때 변경으로 잡힘
- 거리가 기준값보다 크면 변경 (재임베딩, 관련 캐시 무효화)
판정 (학사일정): 임베딩하지 않고 표에서 바로 조회하는 데이터라 행사(event_id)별로 정확히 비교

지문은 정제된 본문 글자만으로 만든다 (수집일 등 메타데이터 제외). 원문 HTML 해시는 요청마다 달라지는 부분 때문에 쓰지 않는다.
중복은 찾아서 리포트에만 남긴다.
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict

from clean import CLEAN, block_text
from common import DATA, REPORTS, load_sources, now_iso, read_json, today, write_json

SNAPSHOTS = DATA / "raw" / "snapshots"
EXPORT = DATA / "export"
EVENT_FIELDS = ("doc_title", "start", "end", "semester", "target_semester", "scope", "url")
BITS = 64


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simhash(text: str, shingle: int) -> int:
    """64비트 SimHash. 공백을 하나로 줄인 글자 n개씩을 특징으로, 등장 횟수를 가중치로 쓴다."""
    t = re.sub(r"\s+", " ", text).strip()
    grams = Counter(t[i:i + shingle] for i in range(max(len(t) - shingle + 1, 1)))
    weights = [0] * BITS
    for gram, w in grams.items():
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(BITS):
            weights[i] += w if (h >> i) & 1 else -w
    return sum(1 << i for i in range(BITS) if weights[i] > 0)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def doc_text(doc: dict) -> str:
    """지문을 만들 본문: 블록 순서대로 소제목 경로와 글자. 못 읽은 블록은 링크로."""
    parts = []
    for b in doc["blocks"]:
        body = block_text(b) or (b.get("src") or "")
        parts.append(" > ".join(b["section_path"]) + "\n" + body)
    return "\n\n".join(parts)


def load_docs(index: dict, shingle: int) -> dict[str, dict]:
    docs = {}
    for path in sorted(CLEAN.glob("*.json")):
        d = read_json(path)
        text = doc_text(d)
        docs[d["doc_id"]] = {
            "sha256": sha256(text),
            "simhash": f"{simhash(text, shingle):016x}",
            "indexed": d["scope"] in index["include_scopes"] and d["doc_id"] not in index["exclude_docs"],
            "blocks": d["blocks"],
        }
    return docs


def load_chunks(shingle: int) -> dict[str, dict] | None:
    """6단계 결과(chunks.jsonl)의 청크별 지문. 청킹 전이면 None."""
    path = EXPORT / "chunks.jsonl"
    if not path.exists():
        return None
    chunks = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            text = (c["section"] or "") + "\n" + (c["text"] or c["url"])
            chunks[c["chunk_id"]] = {"sha256": sha256(text), "simhash": f"{simhash(text, shingle):016x}"}
    return chunks


def load_events() -> dict[str, str]:
    events = {}
    with open(DATA / "calendar.jsonl", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            events[e["event_id"]] = sha256(json.dumps([e[k] for k in EVENT_FIELDS], ensure_ascii=False))
    return events


def compare_docs(before: dict[str, dict], now: dict[str, dict], threshold: int) -> tuple[dict, dict]:
    """색인 대상 문서 비교. 반환: (changes용 목록, 문서별 거리)"""
    result = {"added": [], "modified": [], "skipped": [], "removed": [], "unchanged": 0}
    distances = {}
    for doc_id, cur in now.items():
        old = before.get(doc_id)
        if old is None:
            result["added"].append(doc_id)
        elif old["sha256"] == cur["sha256"]:
            result["unchanged"] += 1
        else:
            dist = hamming(int(old["simhash"], 16), int(cur["simhash"], 16))
            distances[doc_id] = dist
            result["skipped" if dist <= threshold else "modified"].append(doc_id)
    result["removed"] = sorted(k for k in before if k not in now)
    for k in ("added", "modified", "skipped"):
        result[k].sort()
    return result, distances


def compare_exact(old: dict[str, str], new: dict[str, str]) -> dict:
    return {
        "added": sorted(k for k in new if k not in old),
        "modified": sorted(k for k in new if k in old and old[k] != new[k]),
        "removed": sorted(k for k in old if k not in new),
        "unchanged": sum(1 for k in new if old.get(k) == new[k]),
    }


def find_duplicates(docs: dict[str, dict]) -> dict:
    """색인 대상 문서 사이의 중복: 문서 전체가 같은 경우, 같은 소제목 섹션이 여러 문서에 똑같이 있는 경우."""
    whole = defaultdict(list)
    sections = defaultdict(list)
    for doc_id, d in docs.items():
        if not d["indexed"]:
            continue
        texts = [block_text(b) for b in d["blocks"]]
        if any(t.strip() for t in texts):                    # 본문이 빈 문서(iframe만 있음)는 비교하지 않음
            whole[d["sha256"]].append(doc_id)
        by_section = defaultdict(list)
        for b, t in zip(d["blocks"], texts):
            by_section[" > ".join(b["section_path"])].append(t)
        for path, ts in by_section.items():
            joined = "\n".join(ts).strip()
            if len(joined) > 20:
                sections[(path, sha256(joined))].append(doc_id)
    return {
        "same_document": [ids for ids in whole.values() if len(ids) > 1],
        "same_section": sorted(
            ({"section": path or None, "docs": sorted(ids)} for (path, _), ids in sections.items() if len(ids) > 1),
            key=lambda x: -len(x["docs"])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="비교만 하고 지문 기록은 갱신하지 않음")
    args = ap.parse_args()

    src = load_sources()
    index = src["index"]
    cfg = src.get("change_detection", {})
    threshold = cfg.get("simhash_threshold", 3)
    shingle = cfg.get("shingle_chars", 3)

    docs = load_docs(index, shingle)
    events = load_events()
    previous = read_json(SNAPSHOTS / "latest.json", {"created_at": None, "docs": {}, "events": {}})

    # 색인 대상끼리 비교. 설정 변경으로 색인 대상에서 빠지면 '삭제', 새로 들어오면 '추가'
    fp = lambda d: {"sha256": d["sha256"], "simhash": d["simhash"]}
    indexed_now = {k: fp(v) for k, v in docs.items() if v["indexed"]}
    indexed_before = {k: v for k, v in previous["docs"].items() if v.get("indexed")}
    doc_changes, distances = compare_docs(indexed_before, indexed_now, threshold)
    event_changes = compare_exact(previous["events"], events)
    chunks = load_chunks(shingle)
    chunk_changes, chunk_distances = (compare_docs(previous.get("chunks", {}), chunks, threshold)
                                      if chunks is not None else (None, {}))

    run_at = now_iso()
    changes = {
        "schema_version": "1.0",
        "run_at": run_at,
        "previous_run_at": previous["created_at"],
        "simhash_threshold": threshold,
        # added·modified·removed: 다시 처리(재임베딩)하고 관련 캐시 무효화. skipped: 실질적 변경이 아니라 그대로 둠
        "docs": doc_changes,
        "events": event_changes,
        "chunks": chunk_changes,                               # chunk_id 단위. 모델 담당은 added·modified만 다시 임베딩
    }
    total = len(indexed_now)
    reprocess = len(doc_changes["added"]) + len(doc_changes["modified"])
    report = {
        "run_at": run_at,
        "previous_run_at": previous["created_at"],
        "simhash_threshold": threshold,
        "indexed_docs": total,
        # 재임베딩 절감률: 재수집 때 다시 처리하지 않고 건너뛴 문서 비율 (기획안 34p)
        "reembed_saving_ratio": round(1 - reprocess / total, 3) if previous["created_at"] and total else None,
        "chunk_reembed_saving_ratio": (round(1 - (len(chunk_changes["added"]) + len(chunk_changes["modified"])) / len(chunks), 3)
                                       if chunk_changes and previous.get("chunks") and chunks else None),
        "chunks": chunk_changes,
        "chunk_hamming_distance": chunk_distances,
        "docs": doc_changes,
        "hamming_distance": distances,
        "events": event_changes,
        "duplicates": find_duplicates(docs),
        "fingerprints": {k: fp(v) for k, v in docs.items()},
    }
    write_json(REPORTS / f"changes_{today()}.json", report)

    if not args.dry_run:
        write_json(EXPORT / "changes.json", changes)
        snapshot_docs = {k: fp(v) | {"indexed": v["indexed"]} for k, v in docs.items()}
        for doc_id in doc_changes["skipped"]:                  # 건너뛴 문서는 이전 기준 유지 (작은 변경 누적 감지)
            snapshot_docs[doc_id] = previous["docs"][doc_id] | {"indexed": True}
        snapshot_chunks = dict(chunks or {})
        for cid in (chunk_changes or {}).get("skipped", []):    # 건너뛴 청크도 이전 기준 유지
            snapshot_chunks[cid] = previous["chunks"][cid]
        snapshot = {"created_at": run_at, "simhash_threshold": threshold, "docs": snapshot_docs, "events": events,
                    "chunks": snapshot_chunks}
        write_json(SNAPSHOTS / "latest.json", snapshot)
        write_json(SNAPSHOTS / f"{run_at[:19].replace(':', '')}.json", snapshot)

    c = doc_changes
    print(f"비교 기준: {previous['created_at'] or '없음 (첫 실행)'}{'  [dry-run: 기록 갱신 안 함]' if args.dry_run else ''}")
    print(f"색인 대상 문서 {total}개: 추가 {len(c['added'])}, 변경 {len(c['modified'])}, "
          f"건너뜀(거리 ≤ {threshold}) {len(c['skipped'])}, 삭제 {len(c['removed'])}, 그대로 {c['unchanged']}")
    for doc_id, dist in sorted(distances.items(), key=lambda x: -x[1]):
        print(f"  {doc_id}: 거리 {dist} → {'변경' if dist > threshold else '건너뜀'}")
    e = event_changes
    print(f"학사일정 {len(events)}건: 추가 {len(e['added'])}, 변경 {len(e['modified'])}, "
          f"삭제 {len(e['removed'])}, 그대로 {e['unchanged']}")
    if chunk_changes:
        k = chunk_changes
        print(f"청크 {len(chunks)}개: 추가 {len(k['added'])}, 변경 {len(k['modified'])}, 건너뜀 {len(k['skipped'])}, "
              f"삭제 {len(k['removed'])}, 그대로 {k['unchanged']}")
    if report["chunk_reembed_saving_ratio"] is not None:
        print(f"청크 재임베딩 절감률: {report['chunk_reembed_saving_ratio']:.1%}")
    if report["reembed_saving_ratio"] is not None:
        print(f"재임베딩 절감률: {report['reembed_saving_ratio']:.1%}")
    dup = report["duplicates"]
    print(f"중복: 같은 문서 {len(dup['same_document'])}묶음, 여러 문서에 같은 섹션 {len(dup['same_section'])}종류")
    return 0


if __name__ == "__main__":
    sys.exit(main())
