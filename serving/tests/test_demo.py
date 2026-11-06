from starlette.testclient import TestClient

from academic_mcp.demo import create_demo
from test_service import service  # fixture


def test_revision_read_during_cache_write(service):
    revision = service.store.revision()
    with service.store.connect() as writer:
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("INSERT INTO cache VALUES ('held', ?, 9999999999, '{}')", (revision,))
        # Previously, timeout=0 reads raised `database is locked` here.
        assert service.store.revision() == revision


def test_demo_page_and_input_validation():
    with TestClient(create_demo()) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert '같은 요청 10개' in page.text
        bad = client.post('/api/call', json={'tool': 'unknown', 'arguments': {}})
        assert bad.status_code == 400
        cross = client.post('/api/call', json={}, headers={'origin': 'https://unrelated.example'})
        assert cross.status_code == 403
