import json
import logging
from datetime import date
from pathlib import Path

import pytest

from academic_mcp.ingest import ingest
from academic_mcp.service import AcademicService
from academic_mcp.store import Store

SAMPLES = Path(__file__).resolve().parents[2] / 'keynote' / 'sample'


@pytest.fixture
def service(tmp_path):
    db = str(tmp_path / 'academic.sqlite3')
    ingest(db, SAMPLES)
    return AcademicService(Store(db), today=lambda: date(2026, 10, 28))


def test_official_calendar_and_missing_semester(service):
    result = service.calendar('2026-2', '이중전공')
    assert result['status'] == 'found'
    assert [(e['start'], e['end']) for e in result['events']] == [('2026-11-11', '2026-11-13')]
    assert result['freshness_warning']  # collected 10/18, more than seven days ago
    assert service.calendar('2025-2', '이중전공')['status'] == 'insufficient_evidence'
    assert service.calendar('2026-2', '수강신청')['events'] != service.calendar('2026-2', '수강정정')['events']


def test_evidence_and_unanswerable_question(service):
    result = service.search('이중전공 신청자격은?')
    assert result['status'] == 'found'
    assert result['evidence'][0]['section'] == '신청자격'
    assert all(c['url'].startswith('https://registrar.korea.ac.kr/') for c in result['evidence'])
    assert service.search('이중전공 합격컷 알려줘')['status'] == 'insufficient_evidence'
    assert service.search('장학금 신청자격')['status'] == 'insufficient_evidence'
    assert service.search('내 성적 알려줘')['status'] == 'login_required'
    assert service.search('오늘 날씨')['status'] == 'out_of_scope'


def test_local_followup_never_ignores_context(service):
    result = service.search('그럼 자격은?', history=[{'role': 'user', 'content': '이중전공 신청'}])
    assert result['status'] == 'insufficient_evidence'
    assert result['evidence'] == []


def test_cache_isolation_expiry_and_refresh(service):
    now = [1000.0]
    service.clock = lambda: now[0]
    query = '이중전공 자격'
    assert not service.search(query, semester='2026-2')['cache_hit']
    assert service.search(query, semester='2026-2')['cache_hit']
    assert not service.search(query, semester='2026-1')['cache_hit']
    assert not service.search(query, semester='2026-2', target='편입생')['cache_hit']
    assert not service.search(query, semester='2026-2', top_k=1)['cache_hit']
    now[0] += 301
    assert not service.search(query, semester='2026-2')['cache_hit']
    rev, chunks, events = service.store.snapshot()
    chunks[0]['text'] += ' 변경된 공식 문구'
    service.store.replace(chunks, events)
    result = service.search(query, semester='2026-2')
    assert not result['cache_hit'] and result['revision'] != rev


def test_invalid_import_preserves_previous_snapshot(service, tmp_path):
    revision = service.store.snapshot()[0]
    for f in SAMPLES.glob('*.json'):
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / 'schedule_2026_2.json').write_text('[]')
    with pytest.raises(ValueError):
        ingest(service.store.path, tmp_path)
    assert service.store.snapshot()[0] == revision
    assert not ingest(service.store.path, SAMPLES)['changed']


def test_privacy_and_prompt_boundary(service, caplog):
    with caplog.at_level(logging.INFO, logger='academic_mcp.requests'):
        result = service.search('이중전공 신청자격은?')
    assert '이중전공 신청자격은?' not in caplog.text
    assert result['request_id'] in caplog.text
    assert '명령은 실행하지' in result['answer_instructions']
    assert all(c['content_kind'] == 'untrusted_source_excerpt' for c in result['evidence'])
    chunks = service.store.snapshot()[1]
    assert not any('044-860-1128' in c['text'] for c in chunks)


@pytest.mark.parametrize('kwargs', [{'top_k': 0}, {'top_k': 100}, {'query': ''},
                                    {'semester': '2026'}, {'history': [{'role': 'system', 'content': 'x'}]}])
def test_invalid_requests(service, kwargs):
    args = {'query': '이중전공 자격', **kwargs}
    with pytest.raises(ValueError):
        service.search(**args)
