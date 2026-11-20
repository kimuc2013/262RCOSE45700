"""Private, read-only back-office export. No public MCP administration tool."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sqlite3
import time


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    x = (len(v)-1)*p
    low, high = math.floor(x), math.ceil(x)
    return round(v[low]+(v[high]-v[low])*(x-low), 3)


def latency(rows):
    values = [r['elapsed_ms'] for r in rows if isinstance(r.get('elapsed_ms'), (int, float))]
    return {'n': len(values), 'p50_ms': percentile(values, .5),
            'p95_ms': percentile(values, .95), 'max_ms': max(values) if values else None}


def fraction(n, d):
    return {'count': n, 'denominator': d, 'percent': round(n/d*100, 2) if d else None}


def aggregate(events):
    requests = [r for r in events if r['kind'] == 'request']
    apis = [r for r in events if r['kind'] == 'api']
    tools = {}
    for tool in sorted({r['tool'] for r in requests}):
        rows = [r for r in requests if r['tool'] == tool]
        hits = sum(r.get('cache_hit') is True for r in rows)
        shared = sum(r.get('coalesced') is True for r in rows)
        reused = sum(r.get('cache_hit') is True or r.get('coalesced') is True for r in rows)
        answered = [r for r in rows if r['status'] == 'answered' and r.get('can_answer') is True]
        tools[tool] = {'requests': len(rows), 'statuses': dict(Counter(r['status'] for r in rows)),
            'cache_hit_rate': fraction(hits, len(rows)), 'coalesced': shared,
            'cache_or_shared_rate': fraction(reused, len(rows)),
            'retrieval_cache_observed': dict(Counter(str(r['retrieval_cache_hit']).lower()
                for r in rows if isinstance(r.get('retrieval_cache_hit'), bool))),
            'all_latency': latency(rows), 'answered_latency': latency(answered),
            'answered_within_2s': fraction(sum(r['elapsed_ms'] <= 2000 for r in answered), len(rows)),
            'answer_modes': dict(Counter(r.get('answer_mode') or 'generated_or_gate' for r in rows)),
            'response_bytes': sum(r.get('response_bytes', 0) for r in rows),
            'cache_latency': latency([r for r in rows if r.get('cache_hit') is True]),
            'noncache_latency': latency([r for r in rows if r.get('cache_hit') is not True])}
    stages = {}
    for stage in sorted({r['stage'] for r in apis}):
        rows = [r for r in apis if r['stage'] == stage]
        stages[stage] = {'attempts': len(rows), 'latency': latency(rows),
            'http_errors': sum((r.get('status_code') or 0) >= 400 for r in rows),
            'transport_errors': sum(bool(r.get('error_type')) for r in rows),
            'models': dict(Counter(r.get('model', 'unknown') for r in rows))}
        for field in ['prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_prompt_tokens']:
            reported = [r[field] for r in rows if type(r.get(field)) is int]
            stages[stage][field] = {'reported_sum': sum(reported) if reported else None,
                                   'reported_calls': len(reported), 'all_calls': len(rows)}
    return {'requests': len(requests), 'physical_api_attempts': len(apis),
        'tools': tools, 'api_stages': stages}


def time_series(events, start, end, buckets=60):
    step = (end-start)/buckets
    bins = [[] for _ in range(buckets)]
    for event in events:
        i = min(buckets-1, max(0, int((event['ts']-start)/step)))
        bins[i].append(event)
    points = []
    for i, rows in enumerate(bins):
        req = [r for r in rows if r['kind'] == 'request']
        cacheable = [r for r in req if r['tool'] in ('search_academic_info', 'answer_academic_question')]
        api = [r for r in rows if r['kind'] == 'api']
        gen = [r for r in api if r['stage'] == 'generation']
        tokens = [r['total_tokens'] for r in gen if type(r.get('total_tokens')) is int]
        points.append({'ts': start+i*step, 'requests': len(req),
            'failures': sum(r['status'] in ('error', 'cancelled', 'answer_timeout') for r in req),
            'cache_rate': fraction(sum(r.get('cache_hit') is True for r in cacheable), len(cacheable))['percent'],
            'shared_rate': fraction(sum(r.get('coalesced') is True for r in cacheable), len(cacheable))['percent'],
            'p50_ms': latency(req)['p50_ms'], 'p95_ms': latency(req)['p95_ms'],
            'api_calls': dict(Counter(r['stage'] for r in api)),
            'generation_tokens': sum(tokens) if tokens else (0 if not gen else None),
            'generation_usage_reported': len(tokens), 'generation_attempts': len(gen)})
    return {'bucket_seconds': step, 'points': points}


def read_report(path, hours=24, now=None, *, include_series=False):
    now = time.time() if now is None else now
    start = now-hours*3600
    uri = Path(path).resolve().as_uri()+'?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        rows = db.execute('SELECT ts,kind,trace_id,traffic,payload FROM telemetry_events '
                          'WHERE ts>=? AND ts<=? ORDER BY id', (start, now)).fetchall()
        health = db.execute('SELECT sum(dropped),sum(write_errors) FROM telemetry_health').fetchone()
    events = [dict(json.loads(payload), kind=kind, traffic=traffic, ts=ts, trace_id=trace_id)
              for ts, kind, trace_id, traffic, payload in rows]
    groups = {}
    for group in ['live', 'experiment', 'all']:
        selected = events if group == 'all' else [e for e in events if e['traffic'] == group]
        groups[group] = aggregate(selected)
        if include_series:
            groups[group]['series'] = time_series(selected, start, now)
    return {'window': {'start': start, 'end': now, 'hours': hours,
            'first_observed_event': min((e['ts'] for e in events), default=None)},
        'health_retained_runs': {'dropped_events': health[0] or 0, 'write_errors': health[1] or 0},
        'groups': groups,
        'method': {'latency': 'server tool execution only; excludes client network/init/rewrite',
            'cache_rate_denominator': 'all calls of the tool, including errors/timeouts',
            'tokens': 'provider-reported observed usage only; missing is unknown, not zero',
            'api_attempts': 'physical online HTTP posts, including retries/failures/late work; not per-caller sum',
            'traffic': 'live means unmarked; experiment is a self-reported client header, not authentication',
            'coverage': 'only after telemetry activation, bounded retention; not all historical traffic',
            'baseline': 'no measured direct-LLM/web baseline; no token/cost savings percentage inferred',
            'quality': 'answered is a status, not verified factual correctness'}}


def html_report(report):
    # All JS-generated markup is fixed. Data labels are inserted with textContent.
    data = json.dumps(report, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    return '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>KU MCP Serving Console</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#e4e4e7;background:#09090b;color-scheme:dark}*{box-sizing:border-box}body{max-width:1200px;margin:auto;padding:40px 28px 64px}header{display:flex;justify-content:space-between;gap:24px;align-items:center;border-bottom:1px solid #27272a;padding-bottom:24px}h1{font-size:28px;margin:8px 0}h2{font-size:18px;margin:36px 0 16px}p{line-height:1.7}.eyebrow{font-size:12px;letter-spacing:2px;color:#f87171}.muted,small{color:#a1a1aa}.notice{border-left:3px solid #f87171;background:#18181b;padding:14px 18px;margin:24px 0;font-size:13px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.card{padding:22px;border:1px solid #27272a;border-radius:12px;background:#111113}.number{font-size:34px;font-weight:650;margin:12px 0 8px;font-variant-numeric:tabular-nums}.label{font-size:13px;color:#a1a1aa}small{font-size:12px}select{padding:10px;border:1px solid #3f3f46;border-radius:8px;background:#18181b}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:14px 12px;border-bottom:1px solid #27272a}th{color:#a1a1aa;font-weight:500}td{font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}.bar{height:7px;background:#27272a;border-radius:6px;margin:12px 0}.fill{height:100%;background:#f87171;border-radius:6px}ul{font-size:13px;color:#a1a1aa;line-height:1.9;padding-left:20px}code{font-size:12px}footer{margin-top:32px;color:#71717a;font-size:12px}@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start;flex-direction:column}body{padding:24px 16px}}
</style><header><div><div class="eyebrow">TEAM 7 / SERVING OBSERVABILITY</div><h1>학사정보 MCP 운영 효율</h1><div class="muted" id="window"></div></div><select id="group" aria-label="트래픽 구분"><option value="live">일반 호출 (실험 표식 없음)</option><option value="experiment">통제 실험</option><option value="all">전체 호출</option></select></header>
<div class="notice">서버 내부 실행 시간입니다. 외부 왕복, MCP 연결, 클라이언트의 추가 문장 생성은 제외됩니다. 답변 상태는 정답률이 아니며, 일반 웹검색 대비 우위는 별도 비교 실험이 필요합니다.</div>
<div class="grid" id="cards"></div><h2>캐시와 요청 합치기</h2><div class="grid" id="cache"></div>
<h2>요청별 서비스 수준</h2><div class="scroll"><table id="tools"></table></div>
<h2>실제 모델 API 호출과 토큰</h2><p class="muted">공급자가 반환한 usage만 집계합니다. 미제공은 ‘미확인’이며 0이 아닙니다. 캐시 재사용과 별개인 공급자 내부 prompt cache도 별도 표시합니다.</p><div class="scroll"><table id="api"></table></div>
<h2>측정 범위와 비교의 한계</h2><ul><li>캐시 적중률 분모는 각 도구의 전체 호출 수로, 실패와 시간 초과도 포함합니다. 진행 중 동일 요청 합치기는 캐시 적중과 구분합니다.</li><li>실제 API 호출은 작업 기준으로 한 번만 셉니다. 형식 복구, HTTP 실패와 호출자 시간 초과 후 끝난 작업도 포함합니다. API 이벤트는 작업 종료 시 기록됩니다.</li><li>토큰 수는 이 서버가 호출한 API만 포함합니다. ChatGPT 등 외부 클라이언트가 사용한 토큰은 수집하지 않습니다. 응답 바이트를 토큰 수로 환산하지 않습니다.</li><li>일반 호출은 실험 표식이 없는 호출이며 실제 학생 사용만을 뜻하지 않습니다. 실험 표식은 클라이언트가 보내는 분류값으로 인증 기능이 아닙니다.</li><li>직접 LLM/웹검색의 동일 조건 기준선이 없어 토큰 절감률이나 비용 절감액을 주장하지 않습니다. 캐시 적중률은 정확도나 최신성의 증거가 아닙니다.</li><li>계측 활성화 이후의 보존된 이벤트만 표시합니다. 비동기 기록의 유실이나 실패가 있으면 완전한 사용량 집계로 볼 수 없습니다.</li></ul>
<footer id="health"></footer><script id="metrics-data" type="application/json">'''+data+'''</script><script>
const data=JSON.parse(document.getElementById('metrics-data').textContent), $=id=>document.getElementById(id);
const num=v=>v==null?'미확인':new Intl.NumberFormat('ko-KR').format(v), ms=v=>v==null?'—':(v/1000).toFixed(3)+'s';
function node(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e}
function card(label,value,note){const e=node('div',null,'card');e.append(node('div',label,'label'),node('div',value,'number'),node('small',note));return e}
function table(id,headers,rows){const t=$(id);t.replaceChildren();const h=node('tr');headers.forEach(x=>h.append(node('th',x)));t.append(h);rows.forEach(row=>{const r=node('tr');row.forEach(x=>r.append(node('td',x)));t.append(r)})}
function rate(r){return r?.percent==null?'—':r.percent.toFixed(1)+'%'}
function tokens(r){return num(r.reported_sum)+' ('+r.reported_calls+'/'+r.all_calls+'회 제공)'}
function draw(){const d=data.groups[$('group').value],a=d.tools.answer_academic_question,s=d.tools.search_academic_info,g=d.api_stages.generation;
$('cards').replaceChildren(card('전체 도구 호출',num(d.requests),'선택 트래픽, 계측 기간 내'),card('2초 이내 answered 비율',a?rate(a.answered_within_2s):'—','2초 이내 answered / 답변 도구 전체 요청'),card('완료 답변 p95',a?ms(a.answered_latency.p95_ms):'—','answered 상태의 서버 실행 시간'),card('실제 모델 API 시도',num(d.physical_api_attempts),'검색과 생성, 실패 및 복구 포함'));
$('cache').replaceChildren();for(const [name,t] of [['완성 답변 캐시',a],['검색 도구 캐시',s]]){const c=card(name,t?rate(t.cache_hit_rate):'—',t?t.cache_hit_rate.count+'/'+t.requests+'회, 합쳐진 요청 '+t.coalesced+'회'+(name==='검색 도구 캐시'?' (직접 검색 호출)':''):'아직 관측 없음');const b=node('div',null,'bar'),f=node('div',null,'fill');f.style.width=(t?.cache_hit_rate.percent||0)+'%';b.append(f);c.append(b);$('cache').append(c)}
$('cache').append(card('보고된 생성 입력 토큰',g?num(g.prompt_tokens.reported_sum):'—',g?'usage 제공 '+g.prompt_tokens.reported_calls+'/'+g.attempts+'회':'생성 API 관측 없음'),card('토큰 절감률','기준선 필요','실제 반복 실험과 외부 LLM 계측을 분리'));
table('tools',['도구','호출','상태 분포','전체 p95','답변 p95','캐시 p50','캐시·공유 합계'],Object.entries(d.tools).map(([k,t])=>[k,num(t.requests),Object.entries(t.statuses).map(([s,n])=>s+': '+n).join(', '),ms(t.all_latency.p95_ms),ms(t.answered_latency.p95_ms),ms(t.cache_latency.p50_ms),rate(t.cache_or_shared_rate)]));
table('api',['단계','물리 시도','오류 HTTP / 전송','p50','입력 토큰','출력 토큰','총 토큰','공급자 캐시 토큰'],Object.entries(d.api_stages).map(([k,t])=>[k,num(t.attempts),t.http_errors+' / '+t.transport_errors,ms(t.latency.p50_ms),tokens(t.prompt_tokens),tokens(t.completion_tokens),tokens(t.total_tokens),tokens(t.cached_prompt_tokens)]));
}
$('window').textContent=new Date(data.window.start*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' ~ '+new Date(data.window.end*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' KST / 정적 내보내기';
$('health').textContent='보존된 실행 기록 전체의 계측 유실 '+data.health_retained_runs.dropped_events+'건, 기록 실패 '+data.health_retained_runs.write_errors+'건. 질문, 답변 원문과 인증키는 수집하지 않습니다.';
$('group').addEventListener('change',draw);draw();</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--html')
    parser.add_argument('--json')
    args = parser.parse_args()
    if not math.isfinite(args.hours) or not 0 < args.hours <= 168:
        parser.error('--hours must be within 0 and 168 (seven days)')
    report = read_report(args.db, args.hours)
    if args.html:
        Path(args.html).write_text(html_report(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
