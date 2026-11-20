"""Bounded, source-preserving answer context. No model scores or facts are changed."""
import re
from collections import OrderedDict
from .search import normalize, tokenize
from .runtime import checkpoint

POLICY = 'document-context-v3'
# Presentation hints describe evidence boundaries, never assert a domain answer.
GUIDANCE = ('각 문서의 제목과 절에 적힌 대상에만 적용하세요. 일반 규정과 특정 대상의 규정을 합치지 마세요. '
            '질문의 절차, 자격, 예외, 비용의 별도 항목을 함께 확인하세요. '
            '서로 다른 규정이 충돌하면 하나를 임의 선택하지 말고 충돌을 밝히세요. '
            '수집일은 시행일이나 최신 규정 확인일이 아닙니다. 질문과 무관한 부가 사실은 쓰지 마세요.')


def document_key(row):
    # A reused doc_id must not merge URLs or titles with different scope.
    return row.get('doc_id'), row.get('url'), row.get('doc_title')


def build_context(query, evidence, chunks, *, max_chars=6500, max_sections=7):
    groups = OrderedDict()
    for row in evidence:
        groups.setdefault(document_key(row), []).append(row)
    by_doc = {}
    for row in chunks:
        by_doc.setdefault(document_key(row), []).append(row)
    sources, omitted, used = [], [], 0
    reserved = sum(len('['+r.get('section','')+']\n'+(r.get('text') or '')) for r in evidence)
    query_tokens = set(tokenize(query))
    procedural = bool(re.search(r'어떻게|어디|방법|절차|서류|받으려', query))
    conditions = bool(re.search(r'자격|조건|대상', query))
    compact_query = normalize(query)
    def title_match(item):
        terms = [normalize(t.strip()) for t in re.split(r'[>/]', item[1][0].get('doc_title', ''))]
        exact = max((len(t) for t in terms if len(t)>=2 and t in compact_query), default=0)
        leaf_tokens = set(tokenize(terms[-1])) if terms else set()
        return exact + len(query_tokens & leaf_tokens) / max(1,len(leaf_tokens))
    ordered = sorted(groups.items(), key=lambda item: -title_match(item))
    for key, selected in ordered:
        checkpoint()
        first = selected[0]
        def priority(row):
            section = row.get('section', '')
            overlap = len(query_tokens & set(tokenize(section)))
            return overlap + 5 * bool(procedural and re.search(r'방법|절차|신청|제출', section)) + 4 * bool(conditions and re.search(r'자격|대상|예외|기준|원칙|유의', section))
        # Retrieved sections retain relative ordering. Complements are explicitly marked.
        selected_ids = {r['id'] for r in selected}
        def needed(row):
            section = row.get('section', '')
            return (procedural and bool(re.search(r'방법|절차|제출|서류',section))) or (conditions and bool(re.search(r'자격|대상|기준|원칙|유의',section)))
        extra = sorted((r for r in by_doc.get(key, []) if r['id'] not in selected_ids and needed(r)), key=lambda r:(-priority(r),r['id']))
        candidates = selected + extra[:2]
        parts, ids, supplements = [], [], []
        seen = set()
        for row in candidates:
            text = row.get('text')
            if row['id'] in selected_ids:
                reserved -= len('['+row.get('section','')+']\n'+(text or ''))
            if not text or text in seen:continue
            seen.add(text)
            # Entire sections/tables only; never clip a qualification or table row.
            block = '[' + row.get('section', '') + ']\n' + text
            reserve = max(0,reserved) if row['id'] not in selected_ids else 0
            if len(parts) >= max_sections or used + len(block) + reserve > max_chars:
                omitted.append(row['id']);continue
            parts.append(block);ids.append(row['id']);used += len(block)
            if row['id'] not in selected_ids:supplements.append(row['id'])
        if parts:
            sources.append({'url':first['url'],'title':first['doc_title'],
                'section':' / '.join(r.get('section','') for r in selected),
                'text':'적용 범위: '+first['doc_title']+'\n\n'+'\n\n'.join(parts),
                'collected_at':first['collected_at'],'chunk_ids':ids,
                'supplemental_chunk_ids':supplements})
    return {'policy':POLICY,'guidance':GUIDANCE,'sources':sources,
            'omitted_chunk_ids':omitted,'body_chars':used,
            'scope_warning':'수집일만으로 규정의 최신성이나 대상 일치를 보증할 수 없습니다.'}
