"""Offline ingestion, with no changes to keynote samples and no network calls."""
import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .store import Store


def load_records(path: Path, kind: str):
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError(f"{path}: empty or invalid snapshot; keeping existing database")
    required = ("id", "doc_title", "section", "text", "url", "collected_at") if kind == "chunks" else (
        "name", "start", "end", "semester", "source", "collected_at")
    seen = set()
    for record in records:
        if not isinstance(record, dict) or any(not isinstance(record.get(k), str) or not record[k].strip() for k in required):
            raise ValueError(f"{path}: missing required fields")
        url = urlsplit(record["url" if kind == "chunks" else "source"])
        if url.scheme != "https" or url.hostname != "registrar.korea.ac.kr":
            raise ValueError("Only official registrar HTTPS sources are accepted")
        date.fromisoformat(record["collected_at"])
        if kind == "events":
            if date.fromisoformat(record['end']) < date.fromisoformat(record['start']):
                raise ValueError("Event ends before its start")
            if not re.fullmatch(r"\d{4}-[12]", record['semester']):
                raise ValueError("Invalid semester")
        else:
            if record['id'] in seen:
                raise ValueError("Duplicate chunk id")
            seen.add(record['id'])
            # Sample includes a contact section. Do not put personal phone/email into search.
            record['text'] = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[이메일 제거]", record['text'])
            record['text'] = re.sub(r"(?<!\d)0\d{1,2}[- ]\d{3,4}[- ]\d{4}(?!\d)", "[연락처 제거]", record['text'])
    return records


def ingest(db: str, samples: Path):
    # Validate both inputs before opening a write transaction.
    chunks = load_records(samples / "major_double_chunks.json", "chunks")
    events = load_records(samples / "schedule_2026_2.json", "events")
    return Store(db).replace(chunks, events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/academic.sqlite3")
    parser.add_argument("--samples", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(ingest(args.db, args.samples), ensure_ascii=False))


if __name__ == "__main__":
    main()
