from types import SimpleNamespace
import httpx
from academic_mcp import call_metrics as metrics
from academic_mcp.http_timing import HTTPTrace,post


def test_http_trace_includes_inflight_body_but_never_info(monkeypatch):
    now=[10.0];monkeypatch.setattr(metrics,'time',SimpleNamespace(monotonic=lambda:now[0]))
    tracker=metrics.CallProgress();trace=HTTPTrace(tracker,'embedding')
    secret={'url':'private','headers':{'Authorization':'SECRET'}}
    trace('connection.connect_tcp.started',secret);now[0]+=.1;trace('connection.connect_tcp.complete',secret)
    trace('http11.receive_response_body.started',secret);now[0]+=.2
    before=tracker.http_timings()
    assert before['http_elapsed_ms']=={'embedding':{'connect_tcp':100.0,'read_body':200.0}}
    assert before['http_connections']=={'embedding':1}
    trace('http11.receive_response_body.complete',secret);now[0]+=1
    assert tracker.http_timings()==before
    assert 'SECRET' not in repr(before) and 'private' not in repr(before)


def test_trace_counts_bytes_without_changing_request_payload():
    tracker=metrics.CallProgress();token=metrics.progress.set(tracker)
    def handler(req):
        assert req.content==b'{"input":["q"]}'
        assert isinstance(req.extensions['trace'],HTTPTrace)
        return httpx.Response(200,json={'value':3})
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            response=post(client,'embedding','https://model.test/v1',json={'input':['q']})
        assert response.json()=={'value':3}
        assert tracker.http_timings()['http_bytes']['embedding']=={'request':15,'response':11}
    finally:metrics.progress.reset(token)


def test_read_timeout_uses_remaining_total_budget_not_starting_allowance():
    import time
    from academic_mcp.runtime import Budget,current_budget
    budget=Budget(time.monotonic()+.2);token=current_budget.set(budget)
    request=SimpleNamespace(extensions={'timeout':{'read':1.0,'write':1.0,'connect':1.0,'pool':1.0}})
    try:
        trace=HTTPTrace(metrics.CallProgress(),'generation')
        trace('http11.receive_response_headers.started',{'request':request})
        assert all(0<x<=.2 for x in request.extensions['timeout'].values())
        trace('http11.receive_response_headers.complete',{})
    finally:current_budget.reset(token)


def test_serving_pool_preserves_environment_http_proxy(tmp_path,monkeypatch):
    import json,threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from academic_mcp.service import build_service
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            body=json.dumps({'model':'furiosa-ai/bge-m3','data':[{'index':0,'embedding':[0.0,1.0]}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*a):pass
    proxy=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('HTTP_PROXY',f'http://127.0.0.1:{proxy.server_port}')
    monkeypatch.setenv('http_proxy',f'http://127.0.0.1:{proxy.server_port}')
    monkeypatch.setenv('NO_PROXY','');monkeypatch.setenv('no_proxy','')
    monkeypatch.setenv('MODEL_MODE','retrieval')
    monkeypatch.setenv('ACADEMIC_DB',str(tmp_path/'proxy.sqlite3'))
    monkeypatch.setenv('RETRIEVAL_EMBEDDING_URL','http://upstream.invalid/v1/embeddings')
    monkeypatch.setenv('RETRIEVAL_RERANK_URL','http://upstream.invalid/v1/rerank')
    monkeypatch.delenv('RETRIEVAL_API_KEY',raising=False)
    service=None
    try:
        service=build_service()
        assert service.retriever.embed(['q'])==[[0.0,1.0]]
        assert seen==['http://upstream.invalid/v1/embeddings']
    finally:
        if service is not None:service.close()
        proxy.shutdown();proxy.server_close();thread.join()
