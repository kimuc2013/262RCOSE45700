"""Conservative serving-only reuse of ranked evidence, never generated answers."""
import copy
import hashlib
import json
import math
import re
import unicodedata

from .runtime import checkpoint, DataChanged

POLICY = 'guarded-semantic-v1'
# Multiple topic/intent matches are retained, not resolved by precedence.
TOPICS = {
 'leave': r'휴학|학적을 유지하며.*쉬', 'return': r'복학', 'startup': r'창업',
 'childcare': r'육아|아이를 돌', 'military': r'군대|군 복무|전역|입대|군입',
 'withdraw': r'자퇴', 'expulsion': r'제적', 'readmission': r'재입학',
 'retake': r'재수강|다시 수강', 'warning': r'학사경고',
 'giveup_credit': r'학점\s*포기|취득한 학점을 삭제|이수한 학점을 없애',
 'menstrual': r'생리', 'exam': r'기말|시험|결시', 'graduation': r'졸업|학위',
 'early': r'조기', 'honors': r'최우등', 'double': r'이중전공', 'dual': r'복수전공',
 'minor': r'부전공', 'transfer_major': r'전과', 'self_major': r'직접.*전공|전공을.*설계|내가.*전공',
 'micro': r'마이크로디그리', 'undeclared': r'무전공', 'tuition': r'등록금|수업료',
 'installment': r'분할납부|분할 납부|나눠서|나눠 낼|여러 번에 걸쳐',
 'summer': r'계절', 'drop_course': r'수강.*(?:취소|포기)|드랍',
 'cancel_course': r'폐강|수강 인원이 적|수강생이 적|신청 인원이 적',
 'carryover': r'넘길|이월|남겨 둔|미사용', 'card': r'학생증',
 'certificate': r'증명서', 'enrolled_certificate': r'재학증명서',
 'exchange': r'교환학생', 'domestic': r'국내|국내 대학', 'internship': r'현장실습',
 'three_no': r'상대평가|출석부|시험감독', 'short_course': r'8주',
 'english_writing': r'영어.*(?:글|쓰)|영문 글쓰기', 'grade_level': r'2학년',
 'dorm': r'기숙사', 'scholarship': r'국가장학금', 'thesis': r'졸업논문',
 'cutoff': r'커트라인|컷|몇 점이어야.*합격', 'library': r'도서관|열람실',
 'work_study': r'근로장학생', 'cafeteria': r'학생식당', 'graduate': r'대학원|석사',
 'shuttle': r'셔틀버스', 'teaching': r'교직이수', 'reserve': r'예비군', 'club': r'동아리',
}
INTENTS = {
 'duration': r'몇 년|최장 기간|최대.*(?:기간|얼마나)|얼마 동안|기간의 상한|몇 학기.*(?:다닌|다녀|등록)',
 'procedure': r'어떻게|방법|절차|경로|어디서|어느 곳|지원.*기관',
 'eligibility': r'자격|요건|조건|가능|할 수|하는 게|있나|있어',
 'documents': r'서류|증빙', 'timing': r'언제|몇 월|기한|마감|시기|시점|몇 시|시각|일정|시간표',
 'amount': r'몇 학점|학점.*얼마|얼마나 필요|얼마|몇 점|몇 번|몇 회|몇 등급|최고 성적|어느 등급|며칠|일수|몇이야',
}
QUALIFIERS = {
 'freshman':r'신입생', 'transfer_student':r'편입', 'undergrad':r'학부',
 'graduate':r'대학원|석사|박사', 'seoul':r'서울', 'sejong':r'세종',
 'first_term':r'첫\s*(?:번째\s*)?학기', 'during_term':r'학기\s*(?:중|진행|도중)',
 'foreign':r'해외|외국', 'today':r'오늘', 'tomorrow':r'내일', 'current':r'이번|현재|지금',
 'illness':r'질병|아파|아픈|병원', 'pregnancy':r'임신', 'birth':r'출산',
 'international_student':r'외국인|유학생', 'enrolled':r'재학생|재학 중', 'on_leave':r'휴학생|휴학 중',
 'disabled':r'장애', 'finished':r'수료', 'graduating':r'졸업예정|졸업 예정',
 'next':r'다음', 'previous':r'이전|전적', 'negation':r'불가|불가능|금지|않|못|없는|없으면|아닌|제외|안\s*(?:되|돼|한|하)',
}

def signature(query):
    q=unicodedata.normalize('NFKC',query).lower()
    q=q.replace('아홉 번째','9').replace('아홉번째','9')
    topics=tuple(k for k,v in TOPICS.items() if re.search(v,q))
    intents=tuple(k for k,v in INTENTS.items() if re.search(v,q))
    if not topics or not intents:
        return None
    # Preserve all numbers (including dates, credits, semesters), grade letters,
    # organizations/majors, negation and explicit populations. Missing != present.
    numbers=tuple(re.findall(r'\d+(?:\.\d+)?',q))
    quantities=tuple(re.sub(r'\s+','',x) for x in re.findall(r'\d+(?:\.\d+)?\s*(?:학점|학기|년|개월|월|일|주|회|번|점|원|시간|분|%)',q))
    bounds=tuple(re.findall(r'이상|이하|미만|초과|최소|최대|최장|최고|최저|상한|하한',q))
    grades=tuple(re.findall(r'(?<![a-z])[abcdf](?:\+|0|학점|등급|\s|$)',q))
    names=tuple(sorted(set(re.findall(r'[가-힣]+(?:대학교|대학|학과|학부|캠퍼스)|(?<![가-힣])[가-힣]{2,}대(?=\s|의|에서|는|$)',q))))
    qualifiers=tuple(k for k,v in QUALIFIERS.items() if re.search(v,q))
    negatives=tuple(re.findall(QUALIFIERS['negation'],q))
    return (topics,intents,numbers,quantities,bounds,grades,names,qualifiers,negatives)

def normalized(vector):
    if not vector or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):
        raise ValueError('Invalid semantic embedding')
    norm=math.sqrt(sum(v*v for v in vector))
    if not norm or not math.isfinite(norm):raise ValueError('Invalid semantic embedding norm')
    return [v/norm for v in vector]

class SemanticCache:
    def __init__(self,service,threshold=.94,margin=.02):
        self.service,self.threshold,self.margin=service,threshold,margin

    def prepare(self,query,width,revision,semester,target):
        from .retrieval import Retriever
        r=self.service.retriever
        sig=signature(query)
        if not self.service.semantic_cache_enabled or type(r) is not Retriever or not sig or r.min_rerank_score is not None:
            return None
        context=[POLICY,width,revision,self.service.profile(),self.service.today().isoformat(),semester,target,sig]
        bucket='semantic:'+hashlib.sha256(json.dumps(context,sort_keys=True,ensure_ascii=False).encode()).hexdigest()+':'
        # Use the module's validated embedding call. On a miss, give the same
        # vector to a request-local copy so the module never embeds twice.
        vector=normalized(r.embed([query])[0]);checkpoint()
        return bucket,vector

    def lookup(self,prepared):
        if prepared is None:return None
        bucket,vector=prepared;s=self.service
        with s.store.connect(timeout=1) as db:
            rows=db.execute('SELECT payload,expires FROM cache WHERE key>=? AND key<? AND expires>? ORDER BY expires DESC LIMIT 128',
                (bucket,bucket+'~',s.clock())).fetchall()
        candidates=[]
        for payload,expires in rows:
            checkpoint();item=json.loads(payload);v=item['embedding']
            if len(v)!=len(vector):continue
            score=sum(a*b for a,b in zip(vector,v))
            candidates.append((score,item['result'],expires))
        candidates.sort(key=lambda x:x[0],reverse=True)
        if not candidates or candidates[0][0]<self.threshold:return None
        score,result,expires=candidates[0]
        # Near-tied candidates with a different top source are ambiguous.
        top=lambda r:tuple(e['id'] for e in r['evidence'][:1])
        if any(score-other<self.margin and top(result)!=top(r) for other,r,_ in candidates[1:]):return None
        result=copy.deepcopy(result)
        result['semantic_cache']={'similarity':round(score,6),'score_origin':'cached_query','policy':POLICY,'expires_at':expires}
        result['score_type']='cached_'+str(result['score_type'])
        for e in result['evidence']:e['score_type']=result['score_type']
        return result

    def search(self,query,width,*,chunks,revision,prepared):
        r=self.service.retriever
        if prepared is None:return r.search(query,width,chunks=chunks,revision=revision)
        vector=prepared[1]
        r._load_vectors(revision, len(chunks))
        worker=copy.copy(r)
        def embed(texts):
            if texts!=[query]:raise ValueError('Unexpected query embedding request')
            return [vector]
        worker.embed=embed
        return worker.search(query,width,chunks=chunks,revision=revision)

    def publish(self,prepared,key,result,revision):
        if prepared is None or result['status'] not in ('found','evidence_available') or not result['evidence']:return
        s=self.service;bucket,vector=prepared
        with s.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
            if not row or row[0]!=revision:raise DataChanged('Source changed before semantic cache publication')
            checkpoint()
            db.execute('DELETE FROM cache WHERE expires<=?',(s.clock(),))
            if db.execute('SELECT count(*) FROM cache').fetchone()[0]>=1000:
                db.execute('DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires LIMIT 100)')
            # Only fresh retrieval becomes a semantic source. Reused results
            # never become semantic aliases, preventing transitive drift.
            db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',
                (bucket+key,revision,s.clock()+s.ranked_cache.ttl,json.dumps({'embedding':vector,'result':result},ensure_ascii=False)))
