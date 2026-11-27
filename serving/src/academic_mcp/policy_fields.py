"""Typed public policy lookups: complete source bundles, no inferred eligibility."""
import re
from .answer_aliases import polite_query

POLICY='public-policy-fields-v1'
# Document/section identities, never policy numbers or evaluation question IDs.
# Entire-question matches keep dates, populations, negation and extra demands
# outside this route. Values and exceptions always come from the current snapshot.
RULES=(
 ('grade_overview',('성적의 등급',),
  r'(?:성적\s*)?(?P<grade>[a-f](?:\+|0)?)\s*(?:등급)?(?:은|는|의)?평점(?:으로환산하면|이|은)?(?:몇점이야|몇점인가요|값을알려줘)'),
 ('graduation_honors',('종류 및 기준',),
  r'(?:졸업때)?(?:최우등졸업|졸업최우등|최우등)(?:을받으려면|하려면|에요구되는)?평점(?:기준이|기준은|이|은)?(?:얼마여야해|얼마야|얼마인가요|몇점이어야하나요)'),
 ('grade_giveup',('내용','신청대상','포기가능 학점','포기가능 과목','학사운영 규정'),
  r'(?:이미)?(?:받은|취득한|이수한)학점(?:을)?(?:지울수있는|삭제하거나포기하는|없애는)제도(?:가|는)?(?:있어|있나요|있는지궁금해요)'),
 ('grade_missed',('내용','신청절차','제출서류'),
  r'(?:아파서|질병때문에|질병으로)(?:중간|기말)(?:고사|시험)(?:에|을|를)?(?:못보면|응시하지못하면|을결시했을때의)(?:어떻게돼|어떻게처리되나요|처리방법이궁금해요)'),
 ('expelled',('재학연한',),
  r'(?:학교를다닐수있는최대기간|재학할수있는기간에상한|학교에다닐수있는최장재학기간)(?:이)?(?:정해져있어|있나요|정해져있나요)'),
 ('major_transfer',('유의사항',),
  r'전과(?:는|할수있는|허용횟수의)?(?:몇번까지할수있어|최대횟수는몇회인가요|상한이궁금해요)'),
 ('freely_select',('전공 선택',),
  r'무전공(?:으로입학하면전공은|입학생은어느시점에전공을|으로들어온학생의전공결정시기가)(?:언제정해|선택하나요|언제인가요)'),
 ('enroll_divided',('신청대상',),
  r'등록금(?:을)?(?:나눠서낼수있어|분할납부가가능해|여러번에걸쳐나눠낼수있나요)'),
 ('enroll_payment',('납부확인',),
  r'등록금(?:냈는지확인은어디서해|납부여부를확인하는경로는어디인가요|이납부됐는지는어느곳에서확인하나요)'),
 ('registration_no_offered',('내용','폐강기준'),
  r'(?:수강인원이적으면수업이없어지기도해|수강생이적은강좌는폐강되나요|수강생이적다는이유로강의가폐강될수있나요|신청인원이적으면해당수업이취소되기도하나요|인원이적은강좌의폐강기준을알려줘|신청인원이적을때강의가취소되는기준이무엇)'),
 ('registration_step',('수강신청 포기',),
  r'(?:수강신청한과목을학기중에드랍할수있어|학기중에수강중인과목을포기할수있나요|수강신청후학기중과목취소가가능해|학기진행중이미신청한과목의수강을포기할수있나요|수강신청을마친과목을학기중에취소하는게가능해)'),
 ('major_student',('정의','신청자격'),
  r'(?:여러학과과목을섞어서내가직접전공을만들수있어|여러학과의교과목을조합해직접전공을설계할수있나요|내가여러학과수업을묶어전공을만드는게가능해)'),
 ('program_internal_out',('본교 학생의 다른 대학교 수학',),
  r'(?:다른국내대학에서수업듣고학점받을수있어|국내의다른대학강의를수강하고학점인정을받을수있나요|다른국내대학교과목을이수해학점으로인정받는제도가있나요|국내다른대학교에서수강한과목을학점으로인정받을수있나요|다른국내대학수업을듣고학점을취득하는게가능해)'),
 ('info_caution_field',('현장실습 학점 기준 이수 조건',),
  r'현장실습(?:으로)?\s*(?P<credits>\d+)\s*학점(?:받으려면며칠일해야해|을인정받으려면필요한실제근무일수가며칠인가요|을인정받으려면며칠을근무해야하나요|취득에필요한근무일수는얼마야|취득에필요한근무일수는얼마인가요)'),
 ('return',('유의사항',),
  r'(?:군대전역하고언제까지복학해야해|군복무를마친뒤복학해야하는기한은언제인가요|전역후복학신청은언제까지해야하나요)'),
 ('readmission',('신청대상',),
  r'(?:제적당한뒤에다시학교다닐수있나|제적된학생도재입학해서다시다닐수있나요|학교에서제적당한경우다시입학할수있는지궁금해요)'),
 ('major_dual',('대상학과',),
  r'(?:복수전공으로갈수없는학과가있어|복수전공신청이제한되는학과가있나요|어떤학과는복수전공으로선택할수없는지궁금해요)'),
 ('grade_retake',('재수강 성적처리',),
  r'다시수강한과목은최고어느등급까지받을수있나요'),
 ('graduation_require',('졸업요구 학점',),
  r'학위를받으려면학점을모두몇학점채워야하나요'),
 ('grade_excused',('출석 인정사유 및 증빙서류','출석인정 원칙','출석인정 절차'),
  r'(?:생리공결쓰려면어떻게해|생리로인한공결을신청하는절차가어떻게되나요|생리공결을사용하려면어떤절차를밟아야하나요)'),
 ('certificate_issue',('인터넷 제증명 발급',),
  r'(?:재학증명서인터넷으로뽑을수있어|온라인으로재학증명서를발급받을수있나요|인터넷을이용해재학증명서를출력하는게가능해)'),
 ('leave_normal',('등록금 반환',),
  r'(?:학기중간에휴학하면등록금얼마나돌려받아|학기가진행되는도중휴학할때등록금반환금액은어떻게되나요|학기중휴학하면낸등록금의어느정도를환불받나요)'),
 ('flex_semester',('유연학기제',),
  r'(?:수업기간이)?\s*(?P<weeks>\d+)\s*주(?:만하는수업도있어|동안집중적으로운영되는수업도있나요|짜리단기수업이있는지알려줘|인강좌도운영되나요|동안만수강하는형태의수업이있나요)'),
)


def lookup(query,chunks,semester=None,target=None):
    if semester is not None or target is not None:return None
    q=polite_query(query)
    matches=[]
    for doc,sections,pattern in RULES:
        match=re.fullmatch(pattern,q)
        if match:matches.append((doc,sections,match))
    if len(matches)!=1:return None
    doc,sections,match=matches[0];chosen=[]
    for section in sections:
        rows=[r for r in chunks if r.get('doc_id')==doc and r.get('section')==section]
        if len(rows)!=1:return None
        row=rows[0]
        if row.get('extraction_method')=='none' or not row.get('text'):return None
        chosen.append(row)
    if len({r.get('url') for r in chosen})!=1:return None
    text='\n\n'.join(f"{r['section']}\n{r['text']}" for r in chosen)
    if len(text)>1600:return None
    values=match.groupdict()
    if values.get('grade') and not re.search(r'^\|\s*'+re.escape(values['grade'])+r'\s*\|',text,re.M|re.I):return None
    if values.get('credits') and not any(re.search(r'(?<!\d)'+re.escape(values['credits'])+r'\s*학점(?!\d)',line.split('|')[1])
            for line in text.splitlines() if re.match(r'^\|[^|]+\|',line) and '이수조건' not in line):return None
    if values.get('weeks'):
        offered=re.search(r'((?:\d+\s*주\s*[,·]\s*)*\d+\s*주)의?\s*집중형',text)
        if not offered or values['weeks']+'주' not in re.findall(r'\d+주',re.sub(r'\s+','',offered[1])):return None
    return {'answer':f"공식 안내 ({chosen[0]['doc_title']})\n{text}",
            'citations':[chosen[0]['url']],'chunk_ids':[r['id'] for r in chosen],
            'answer_mode':'source_items' if len(chosen)>1 else 'source_item','can_answer':True}
