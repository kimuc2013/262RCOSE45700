from academic_mcp.metrics import aggregate,time_series
from academic_mcp.call_metrics import response_metrics


def row(**kw):
    return dict(kind='request',tool='answer_academic_question',status='answered',can_answer=True,
                elapsed_ms=1,model_calls_complete=True,model_calls=0,ts=1,**kw)


def test_model_free_metric_denominator_excludes_refusals_but_keeps_unknown_success():
    rows=[row(cache_hit=True),row(answer_mode='source_item'),row(answer_cache_kind='verified_alias',cache_hit=True),
          {**row(), 'model_calls':1},row(coalesced=True,shared_model_calls=3),
          {**row(),'model_calls':None},row(coalesced=True),
          {**row(),'status':'answer_timeout','model_calls_complete':False},
          {**row(),'status':'insufficient_evidence','can_answer':False},
          {**row(),'status':'error'}]
    tool=aggregate(rows)['tools']['answer_academic_question']
    assert tool['model_free_response_rate']=={'count':3,'denominator':7,'percent':42.86}
    assert tool['model_free_measurement_coverage']['count']==5
    assert tool['model_free_reasons']=={'response_cache':1,'direct_source':1,'verified_alias_cache':1}
    assert time_series(rows,0,2,buckets=1)['points'][0]['model_free_response_rate']==42.86


def test_shared_physical_work_and_timeout_never_count_as_model_free():
    assert response_metrics({'generation_calls':1},1,shared=True)['model_free_response'] is False
    assert response_metrics({},1,shared=True)['model_free_response'] is True
    assert response_metrics({},1,complete=False,observed=True)['model_free_response'] is None
    tool=aggregate([row(coalesced=True,shared_model_calls=0)])['tools']['answer_academic_question']
    assert tool['model_free_response_rate']['percent']==100
    assert tool['model_free_reasons']=={'shared_no_model':1}


def test_partial_retrieval_or_embedding_sharing_is_unknown_without_physical_total():
    for stage in ['retrieval_coalesced','embedding_coalesced']:
        m=response_metrics({stage:True},1)
        assert m['model_free_response'] is None and m['shared_model_work_unknown']
        tool=aggregate([{**row(),**m}])['tools']['answer_academic_question']
        assert tool['model_free_response_rate']['count']==0
        assert tool['model_free_measurement_coverage']['count']==0
        assert response_metrics({stage:True,'generation_calls':1},1)['model_free_response'] is False
