import hashlib
import json
from pathlib import Path

import pytest

from academic_mcp.snapshot import ingest_snapshot, load_snapshot
from academic_mcp.store import Store
from test_service import service  # noqa: F401


@pytest.fixture
def snapshot(tmp_path):
    chunks=[{'chunk_id':'card#1','doc_id':'card','doc_title':'학생증',
             'section':'','text':'발급 문의: 학생지원팀 02-1234-5678',
             'url':'https://registrar.korea.ac.kr/eduinfo/affairs/card.do',
             'collected_at':'2026-11-12','extraction_method':'html','embedding':None}]
    events=[{'event_id':'event1','doc_title':'2027학년도 수강신청','start':'2027-02-01','end':'2027-02-02',
             'semester':'2026-2','target_semester':'2027-1','collected_at':'2026-11-12',
             'url':'https://registrar.korea.ac.kr/eduinfo/affairs/schedule.do'}]
    manifest={'schema_version':'1.0','dataset_version':'test-v1','embedding':{
        'model':'furiosa-ai/bge-m3','revision':'test','dimension':3},'files':{}}
    def write(chunks=chunks,events=events):
        for kind,name,rows in [('chunks','chunks.jsonl',chunks),('calendar','calendar.jsonl',events)]:
            raw=('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n').encode()
            (tmp_path/name).write_bytes(raw)
            manifest['files'][kind]={'path':name,'count':len(rows),'sha256':hashlib.sha256(raw).hexdigest()}
        (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    write()
    return tmp_path,chunks,events,manifest,write


def test_real_contract_preserves_context_and_declares_missing_embeddings(snapshot,service):
    path,_,_,_,_=snapshot
    old=service.search('이중전공 자격')['revision']
    result=ingest_snapshot(service.store.path,path)
    assert result['chunks']==result['events']==1
    assert result['embedded_chunks']==0 and result['revision']!=old
    revision,chunks,events=service.store.snapshot()
    assert chunks[0]['id']=='card#1' and chunks[0]['section']=='학생증'
    assert chunks[0]['section_original']==''
    assert '02-1234-5678' in chunks[0]['text']  # Preserve public office contacts.
    assert events[0]['name']=='2027학년도 수강신청'
    assert events[0]['semester']=='2026-2' and events[0]['target_semester']=='2027-1'
    assert events[0]['source']==events[0]['url']
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM cache').fetchone()[0]==0
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='dataset'").fetchone()[0])['dataset_version']=='test-v1'
    assert ingest_snapshot(service.store.path,path)['changed'] is False


@pytest.mark.parametrize('failure',['hash','count','url','duplicate','dimension','empty','semester','path'])
def test_invalid_export_never_changes_live_data(snapshot,service,failure):
    path,chunks,events,manifest,write=snapshot
    old=service.store.snapshot()
    if failure=='url':chunks[0]['url']='https://example.com/untrusted'
    if failure=='duplicate':chunks.append(dict(chunks[0]))
    if failure=='dimension':chunks[0]['embedding']=[.1,.2]
    if failure=='empty':chunks.clear()
    if failure=='semester':events[0]['target_semester']='2027-3'
    write()
    if failure=='hash':(path/'chunks.jsonl').write_text('{}\n')
    if failure=='count':manifest['files']['calendar']['count']+=1
    if failure=='path':manifest['files']['chunks']['path']='../chunks.jsonl'
    if failure in ('count','path'):(path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):ingest_snapshot(service.store.path,path)
    assert service.store.snapshot()==old


def test_unreadable_records_never_become_answer_evidence(snapshot):
    path,chunks,events,_,write=snapshot
    chunks.append({**chunks[0],'chunk_id':'unreadable','extraction_method':'none','text':None})
    write()
    loaded,_,meta=load_snapshot(path)
    assert len(loaded)==1 and meta['excluded_title_only_chunks']==1


def test_dataset_metadata_change_invalidates_existing_cache(snapshot,service):
    path,_,_,manifest,write=snapshot
    first=ingest_snapshot(service.store.path,path)
    with service.store.connect() as db:
        db.execute('INSERT INTO cache VALUES (?,?,?,?)',('key',first['revision'],9999999999,'{}'))
    manifest['dataset_version']='test-v2';write()
    assert ingest_snapshot(service.store.path,path)['revision']!=first['revision']
    with service.store.connect() as db:assert db.execute('SELECT count(*) FROM cache').fetchone()[0]==0
