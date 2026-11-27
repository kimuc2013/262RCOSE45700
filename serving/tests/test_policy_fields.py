import pytest
from academic_mcp.policy_fields import lookup


def row(doc,section,text,ident=None):
    return {'id':ident or section,'doc_id':doc,'section':section,'doc_title':'테스트용 공식 항목',
            'text':text,'url':'https://official.test/'+doc,'extraction_method':'html'}


def test_grade_lookup_uses_current_table_values_and_retains_notes():
    data=[row('grade_overview','성적의 등급','[표]\n| 등급 | 평점 |\n| A+ | 9.99 |\n| A | 8.88 |\n적용 조건 원문')]
    result=lookup('A+는 평점이 몇 점이야?',data)
    assert '9.99' in result['answer'] and '적용 조건 원문' in result['answer']
    assert lookup('B+는 평점이 몇 점이야?',data) is None
    assert lookup('2024년 A+는 평점이 몇 점이야?',data) is None
    assert lookup('내 A+는 평점이 몇 점이야?',data) is None
    assert lookup('A+는 평점이 몇 점이야?',data,semester='2024-1') is None


def test_exam_lookup_requires_all_conditions_procedure_and_documents():
    data=[row('grade_missed','내용','시험 불응시는 담당 교원의 재량'),
          row('grade_missed','신청절차','사유 종료 후 정해진 기간 내 제출'),
          row('grade_missed','제출서류','종합병원 진단서 등 증빙 필수')]
    for q in ['아파서 기말고사를 못 보면 어떻게 돼?','질병으로 중간시험을 못 보면 어떻게 돼?']:
        result=lookup(q,data)
        assert result['can_answer'] and len(result['chunk_ids'])==3
        assert all(r['text'] in result['answer'] for r in data)
    assert lookup('아파서 기말고사를 못 보면 어떻게 돼?',data[:1]) is None
    assert lookup('기말고사 끝나고 12일 지난 뒤 아파서 못 보면 어떻게 돼?',data) is None


def test_credit_lookup_checks_key_column_not_digits_elsewhere():
    data=[row('info_caution_field','현장실습 학점 기준 이수 조건',
              '| 학점 | 이수조건 |\n| 3학점 | 다른 문구 6학점 |\n| 12학점, 15학점 | 실제 근무일 99일 이상 |')]
    assert lookup('현장실습 6학점 받으려면 며칠 일해야 해?',data) is None
    result=lookup('현장실습 15학점 받으려면 며칠 일해야 해?',data)
    assert '99일 이상' in result['answer']
    assert lookup('현장실습 1 2학점 받으려면 며칠 일해야 해?',data) is None
    assert lookup('현장실습 1.5학점 받으려면 며칠 일해야 해?',data) is None
    assert lookup('현장실습 6학점이면 3학점보다 며칠 더 일해야 해?',data) is None


def test_week_lookup_does_not_mistake_old_course_length_for_offered_duration():
    data=[row('flex_semester','유연학기제','기존 16주 교육과정을 탈피하여 희망 강좌를 4주, 8주, 10주의 집중형 강의로 운영')]
    assert lookup('8주만 하는 수업도 있어?',data)['can_answer']
    assert lookup('10주만 하는 수업도 있어?',data)['can_answer']
    assert lookup('16주짜리 단기 수업이 있는지 알려줘.',data) is None
    assert lookup('18주만 하는 수업도 있어?',data) is None


def test_bundles_reject_duplicates_unreadable_or_extra_demands():
    data=[row('enroll_divided','신청대상','일부 대상 제외, 예외 조건 전체')]
    assert lookup('등록금을 나눠서 낼 수 있어?',data)['answer'].endswith(data[0]['text'])
    assert lookup('등록금을 나눠서 낼 수 있어?',data+data) is None
    assert lookup('등록금을 나눠서 낼 수 있어? 그리고 자격은?',data) is None
    assert lookup('편입생인데 등록금을 나눠서 낼 수 있어?',data) is None
    assert lookup('등록금을 나눠서 낼 수 있어?',[dict(data[0],extraction_method='none')]) is None
