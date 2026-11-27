"""Content-free httpcore timings. Never retain trace info, URLs or headers."""
import httpx
from .call_metrics import progress
from .runtime import checkpoint, current_budget

EVENTS={
 'connection.connect_tcp':'connect_tcp', 'connection.start_tls':'tls',
 'http11.send_request_headers':'send_headers','http11.send_request_body':'send_body',
 'http11.receive_response_headers':'wait_headers','http11.receive_response_body':'read_body',
 'http2.send_request_headers':'send_headers','http2.send_request_body':'send_body',
 'http2.receive_response_headers':'wait_headers','http2.receive_response_body':'read_body',
}


class HTTPTrace:
    def __init__(self,tracker,stage):
        self.tracker,self.stage,self.active=tracker,stage,{}

    def __call__(self,name,info):
        event,_,phase=name.rpartition('.')
        field=EVENTS.get(event)
        if field is None:return
        if phase=='started':
            checkpoint()
            budget=current_budget.get()
            request=info.get('request')
            if budget is not None and request is not None:
                # httpx timeouts are per phase. Re-clamp after connect/upload so
                # a read cannot reuse the larger, now-stale starting allowance.
                left=budget.remaining()
                timeouts=request.extensions.get('timeout',{})
                for kind in ('connect','write','read','pool'):
                    value=timeouts.get(kind)
                    timeouts[kind]=left if value is None else min(value,left)
            self.active[event]=self.tracker.begin('http.'+self.stage+'.'+field)
            if field=='connect_tcp':self.tracker.http_connect(self.stage)
        elif phase in ('complete','failed') and event in self.active:
            self.tracker.end(self.active.pop(event))


def post(client,stage,*args,**kwargs):
    tracker=progress.get()
    base=client
    while hasattr(base,'client'):base=base.client
    if tracker is None or not isinstance(base,httpx.Client):return client.post(*args,**kwargs)
    extensions=dict(kwargs.get('extensions') or {})
    if 'trace' not in extensions:extensions['trace']=HTTPTrace(tracker,stage)
    kwargs['extensions']=extensions
    response=client.post(*args,**kwargs)
    # These are lengths only; request/response contents never enter telemetry.
    tracker.http_sizes(stage,len(response.request.content),len(response.content))
    return response
