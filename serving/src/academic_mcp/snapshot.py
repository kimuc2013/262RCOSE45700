"""Validate a complete backfill export before atomically replacing serving data.

No crawling, chunk generation, model API or S3 writes occur here. Downloading
is deliberately separate so an interrupted download cannot publish partial data.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .ingest import validate_records
from .store import Store

METHODS = {'html', 'pdf_text', 'hwp', 'vlm', 'none'}


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing {name}')
    return value


def semester(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'\d{4}-[12]', value)):
        raise ValueError('Invalid target_semester')


def read_file(directory, info, filename):
    if not isinstance(info, dict) or info.get('path') != filename:
        raise ValueError(f'Expected manifest path {filename}')
    raw = (directory / filename).read_bytes()
    if hashlib.sha256(raw).hexdigest() != info.get('sha256'):
        raise ValueError(f'SHA256 mismatch: {filename}; existing data retained')
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    count = info.get('count')
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or len(rows) != count:
        raise ValueError(f'Count mismatch or empty file: {filename}')
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError(f'Expected objects: {filename}')
    return rows


def load_snapshot(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != '1.0':
        raise ValueError('Unsupported snapshot schema_version')
    nonempty(manifest.get('dataset_version'), 'dataset_version')
    embedding = manifest.get('embedding')
    if not isinstance(embedding, dict):
        raise ValueError('Missing embedding metadata')
    nonempty(embedding.get('model'), 'embedding.model')
    nonempty(embedding.get('revision'), 'embedding.revision')
    dimension = embedding.get('dimension')
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError('Invalid embedding dimension')
    files = manifest.get('files')
    if not isinstance(files, dict):
        raise ValueError('Missing manifest files')
    raw_chunks = read_file(directory, files.get('chunks'), 'chunks.jsonl')
    raw_events = read_file(directory, files.get('calendar'), 'calendar.jsonl')
    chunks, events, seen, embedded, skipped = [], [], set(), 0, 0
    for row in raw_chunks:
        chunk_id = nonempty(row.get('chunk_id'), 'chunk_id')
        if chunk_id in seen:
            raise ValueError('Duplicate chunk_id')
        seen.add(chunk_id)
        nonempty(row.get('doc_id'), 'doc_id')
        nonempty(row.get('doc_title'), 'doc_title')
        method = row.get('extraction_method')
        if method not in METHODS:
            raise ValueError('Invalid extraction_method')
        semester(row.get('target_semester'))
        vector = row.get('embedding')
        if vector is not None:
            if not isinstance(vector, list) or len(vector) != dimension or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
                raise ValueError('Invalid embedding vector')
        if method == 'none':
            skipped += 1
            continue  # Title-only links are never treated as text evidence.
        nonempty(row.get('text'), 'text')
        section = row.get('section')
        if section is not None and not isinstance(section, str):
            raise ValueError('Invalid section')
        # Empty headings occur in the real export; retain the original value.
        chunks.append({**row, 'id': chunk_id, 'section_original': section,
                       'section': section or row['doc_title']})
        embedded += vector is not None
    seen = set()
    for row in raw_events:
        event_id = nonempty(row.get('event_id'), 'event_id')
        if event_id in seen:
            raise ValueError('Duplicate event_id')
        seen.add(event_id)
        semester(row.get('target_semester'))
        events.append({**row, 'name': row.get('doc_title'), 'source': row.get('url')})
    # Team backfill already applies its privacy policy, preserving public office
    # contact details. Do not silently strip those again in this adapter.
    validate_records(chunks, 'chunks', directory, redact_contacts=False)
    validate_records(events, 'events', directory, redact_contacts=False)
    metadata = {**manifest, 'searchable_chunks': len(chunks),
                'embedded_chunks': embedded, 'excluded_title_only_chunks': skipped}
    return chunks, events, metadata


def ingest_snapshot(db, directory):
    chunks, events, metadata = load_snapshot(directory)
    result = Store(db).replace(chunks, events, metadata=metadata)
    return {**result, 'dataset_version': metadata['dataset_version'],
            'embedded_chunks': metadata['embedded_chunks'],
            'excluded_title_only_chunks': metadata['excluded_title_only_chunks']}
