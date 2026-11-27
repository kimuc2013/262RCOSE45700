import * as React from "react"
import { createRoot, type Root } from "react-dom/client"
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts"
import { ChartContainer, ChartLegend, ChartTooltip, ChartTooltipContent, type ChartConfig } from "./components/ui/chart"
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "./components/ui/card"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./components/ui/select"

type Point = { ts:number; interval_start:number; interval_end:number; partial:boolean; requests:number; failures:number; nonfailures:number; cache_samples:number; latency_samples:number; model_free_samples:number; model_free_measured:number; api_calls:Record<string,number>; [key:string]:any }
type Series = { key:string; label:string; color:string; numerator?:string }
type Spec = { id:string; title:string; description:string; kind:"bar"|"dots"|"area"; unit:string; samples:string; percent?:boolean; stacked?:boolean; series:Series[] }
const specs:Spec[] = [
  {id:"requests",title:"요청량과 실패",description:"실패 외 호출과 실패의 합계가 전체 요청량입니다",kind:"bar",unit:"회",samples:"requests",stacked:true,series:[{key:"nonfailures",label:"실패 외 호출",color:"var(--chart-1)"},{key:"failures",label:"오류·취소·시간 초과",color:"var(--chart-failure)"}]},
  {id:"model-free",title:"모델 무호출 응답률",description:"완료 답변 중 공유 작업까지 서버 모델 호출 0회인 비율",kind:"dots",unit:"%",samples:"model_free_samples",percent:true,series:[{key:"model_free_response_rate",label:"모델 무호출",color:"var(--chart-1)",numerator:"model_free_count"}]},
  {id:"cache",title:"캐시와 요청 공유",description:"검색·답변 도구의 비율, 중복될 수 있는 지표를 각각 표시합니다",kind:"dots",unit:"%",samples:"cache_samples",percent:true,series:[{key:"cache_rate",label:"응답 캐시",color:"var(--chart-1)",numerator:"cache_hits"},{key:"effective_cache_rate",label:"검색 재사용 포함",color:"var(--chart-2)",numerator:"effective_cache_hits"},{key:"semantic_cache_rate",label:"의미 캐시",color:"var(--chart-3)",numerator:"semantic_cache_hits"},{key:"shared_rate",label:"요청 공유",color:"var(--chart-4)",numerator:"shared_requests"}]},
  {id:"latency",title:"서버 처리 시간",description:"구간별 요청의 p50과 p95, 목표 2초 · 외부 왕복 제외",kind:"dots",unit:"초",samples:"latency_samples",series:[{key:"p50",label:"p50",color:"var(--chart-1)"},{key:"p95",label:"p95",color:"var(--chart-2)"}]},
  {id:"api",title:"실제 모델 API 호출",description:"임베딩, 리랭킹, 생성의 물리 시도 합계",kind:"bar",unit:"회",samples:"api_samples",stacked:true,series:[{key:"embedding",label:"임베딩",color:"var(--chart-1)"},{key:"rerank",label:"리랭킹",color:"var(--chart-2)"},{key:"generation",label:"생성",color:"var(--chart-3)"}]},
  {id:"tokens",title:"생성 API가 보고한 총 토큰",description:"usage 미제공 구간은 미확인입니다",kind:"area",unit:"토큰",samples:"generation_attempts",series:[{key:"generation_tokens",label:"보고된 총 토큰",color:"var(--chart-1)"}]},
]
const stamp = (ts:number,long=false) => new Date(ts*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",...(long?{month:"2-digit",day:"2-digit"}:{}),hour:"2-digit",minute:"2-digit",hour12:false})
const number=(v:number,unit:string) => unit==="초"?v.toFixed(3):new Intl.NumberFormat("ko-KR",{maximumFractionDigits:1}).format(v)
const count=(v:any)=>typeof v==="number"?new Intl.NumberFormat("ko-KR").format(v):"미확인"
function MetricChart({spec,points,hours,bucket}:{spec:Spec;points:Point[];hours:string;bucket:string}) {
  const [hidden,setHidden]=React.useState<string[]>([])
  const data=React.useMemo(()=>points.map(p=>({...p,p50:p.p50_ms==null?null:p.p50_ms/1000,p95:p.p95_ms==null?null:p.p95_ms/1000,embedding:p.api_calls.embedding??0,rerank:p.api_calls.rerank??0,generation:p.api_calls.generation??0,api_samples:Object.values(p.api_calls).reduce((n,v)=>n+v,0)})),[points])
  const config=Object.fromEntries(spec.series.map(s=>[s.key,{label:s.label,color:s.color}])) satisfies ChartConfig
  const visible=spec.series.filter(s=>!hidden.includes(s.key))
  const long=points.length>1&&points.at(-1)!.ts-points[0].ts>43200
  const Chart=spec.kind==="bar"?BarChart:spec.kind==="area"?AreaChart:LineChart
  const featured=spec.id==="requests"
  const samples=data.every(p=>typeof p[spec.samples]==="number")?data.reduce((n,p)=>n+p[spec.samples],0):null
  const observed=data.some(p=>visible.some(s=>p[s.key]!=null))
  function setControl(id:string,value:string) {const control=document.getElementById(id) as HTMLSelectElement;control.value=value;control.dispatchEvent(new Event("change",{bubbles:true}))}
  function dot(key:string,isolated=false) {
    return (props:any) => {
      if(props.value==null||!Number.isFinite(props.cx)||!Number.isFinite(props.cy))return <g key={`${key}-${props.index}`}/>
      // Equal summaries retain their real interval position; the tooltip lists all series.
      if(isolated&&((props.index>0&&data[props.index-1]?.[key]!=null)||(props.index+1<data.length&&data[props.index+1]?.[key]!=null)))return <g key={`${key}-${props.index}`}/>
      return <circle key={`${key}-${props.index}`} data-metric-point={key} cx={props.cx} cy={props.cy} r={3.3} fill={`var(--color-${key})`} stroke="var(--card)" strokeWidth={.8}/>
    }
  }
  function tooltip(props:any) {
    if(!props.active)return null
    const point=props.payload?.[0]?.payload??data.find(p=>p.ts===Number(props.label))
    if(!point)return null
    const payload=visible.map(s=>({name:s.key,dataKey:s.key,value:point[s.key]??null,color:s.color,payload:point,type:"line"}))
    return <ChartTooltipContent active payload={payload as any} indicator="dot" labelFormatter={()=><div className="grid gap-1"><div>{stamp(point.interval_start??point.ts,true)} ~ {stamp(point.interval_end??point.ts+Number(bucket)*60)} KST{point.partial?" (부분 구간)":""}</div><div className="text-muted-foreground">{spec.id==="api"?"API 시도":spec.id==="tokens"?"생성 API 시도":spec.id==="cache"?"검색·답변 표본":spec.id==="model-free"?"완료 답변 표본":spec.id==="latency"?"처리 시간 표본":"전체 호출"} {count(point[spec.samples])}{spec.id==="api"||spec.id==="tokens"?"회":"건"}{point[spec.samples]===0?" · 관측 없음":""}</div>{spec.id==="model-free"&&<div className="text-muted-foreground">모델 호출 계측 {count(point.model_free_measured)}/{count(point.model_free_samples)}건</div>}{spec.id==="latency"&&point.latency_samples===1&&<div className="text-muted-foreground">표본 1건: p50과 p95가 같습니다</div>}</div>} formatter={(value,name)=>{const s=spec.series.find(s=>s.key===String(name));return <><div className="h-2.5 w-2.5 shrink-0 rounded-[2px]" style={{background:s?.color}}/><span className="text-muted-foreground">{s?.label??String(name)}</span><span className="ml-auto font-mono font-medium tabular-nums">{value==null?"관측 없음":`${number(Number(value),spec.unit)} ${spec.unit}`}{value!=null&&s?.numerator?` (${count(point[s.numerator])}/${count(point[spec.samples])}건)`:""}</span></>}}/>
  }
  return <Card data-chart-kind={spec.kind} data-chart-library="shadcn-recharts" data-featured={featured} data-chart-id={spec.id} className={featured?"pt-0":""}>
    <CardHeader className={featured?"flex items-center gap-2 space-y-0 border-b py-5 sm:flex-row":""}>
      <div className="grid flex-1 gap-1"><CardTitle>{spec.title}</CardTitle><CardDescription>{spec.description}</CardDescription></div>
      {featured&&<div className="flex flex-wrap gap-2"><Select value={hours} onValueChange={v=>setControl("hours",v)}><SelectTrigger className="w-[160px] rounded-lg" aria-label="차트 조회 기간"><SelectValue/></SelectTrigger><SelectContent className="rounded-xl"><SelectItem value="0.25">최근 15분</SelectItem><SelectItem value="1">최근 1시간</SelectItem><SelectItem value="24">최근 24시간</SelectItem></SelectContent></Select><Select value={bucket} onValueChange={v=>setControl("bucket_minutes",v)}><SelectTrigger className="w-[128px] rounded-lg" aria-label="차트 집계 간격"><SelectValue/></SelectTrigger><SelectContent className="rounded-xl"><SelectItem value="1">1분 집계</SelectItem><SelectItem value="5">5분 집계</SelectItem><SelectItem value="15">15분 집계</SelectItem></SelectContent></Select></div>}
    </CardHeader>
    <CardContent className={featured?"px-2 pt-4 sm:px-6 sm:pt-6":""}>
      {!points.length||(!observed&&spec.kind==="dots")?<div className="chart-empty">선택 기간에 집계 가능한 {spec.id==="model-free"?"완료 답변":"요청"}이 없습니다</div>:<ChartContainer id={`ops-${spec.id}`} config={config} className="aspect-auto h-[250px] w-full" aria-label={spec.title}>
        <Chart accessibilityLayer data={data} margin={{left:0,right:18,top:12,bottom:0}}>
          <CartesianGrid vertical={false}/>
          <YAxis domain={spec.percent?[0,100]:[0,"auto"]} ticks={spec.percent?[0,25,50,75,100]:undefined} allowDecimals={spec.unit==="초"||!!spec.percent} tickLine={false} axisLine={false} width={48} padding={{top:8,bottom:8}} tickFormatter={v=>spec.percent?`${v}%`:spec.unit==="초"?`${Number(v).toFixed(1)}s`:count(v)}/>
          <XAxis dataKey="ts" type="number" domain={["dataMin","dataMax"]} tickLine={false} axisLine={false} tickMargin={8} minTickGap={32} padding={{left:16,right:16}} tickFormatter={v=>stamp(Number(v),long)}/>
          <ChartTooltip cursor={false} filterNull={false} content={tooltip}/>
          <ChartLegend content={()=><div className="flex flex-wrap items-center justify-center gap-4 pt-3" aria-label={`${spec.title} 범례`}>{spec.series.map(s=><button type="button" key={s.key} className="chart-legend-item" aria-pressed={!hidden.includes(s.key)} onClick={()=>setHidden(h=>h.includes(s.key)?h.filter(k=>k!==s.key):h.length<spec.series.length-1?[...h,s.key]:h)}><div className="h-2 w-2 shrink-0 rounded-[2px]" style={{background:s.color}}/>{s.label}</button>)}</div>}/>
          {spec.id==="latency"&&<ReferenceLine y={2} ifOverflow="extendDomain" stroke="var(--chart-target)" strokeDasharray="4 4" label={{value:"목표 2초",position:"insideTopRight",fill:"var(--muted-foreground)",fontSize:11}}/>}
          {visible.map(s=>spec.kind==="bar"?<Bar key={s.key} dataKey={s.key} fill={`var(--color-${s.key})`} radius={[2,2,0,0]} stackId={spec.stacked?"counts":undefined} maxBarSize={32} isAnimationActive={false}/>:spec.kind==="area"?<Area key={s.key} type="linear" dataKey={s.key} fill={`url(#fill-${spec.id}-${s.key})`} stroke={`var(--color-${s.key})`} strokeWidth={1} connectNulls={false} dot={dot(s.key,true)} isAnimationActive={false}/>:<Line key={s.key} type="linear" dataKey={s.key} stroke={`var(--color-${s.key})`} strokeWidth={0} connectNulls={false} dot={dot(s.key)} activeDot={{r:5}} isAnimationActive={false}/>)}
          <defs>{spec.series.map(s=><linearGradient key={s.key} id={`fill-${spec.id}-${s.key}`} x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={s.color} stopOpacity={.8}/><stop offset="95%" stopColor={s.color} stopOpacity={.1}/></linearGradient>)}</defs>
        </Chart>
      </ChartContainer>}
    </CardContent>
    <CardFooter className="flex-col items-start gap-2 text-sm"><div className="font-medium leading-none" data-sample-summary>{bucket}분 집계 · {spec.id==="api"||spec.id==="tokens"?"API 시도":"선택 기간 표본"} {count(samples)}{spec.id==="api"||spec.id==="tokens"?"회":"건"}</div><div className="leading-none text-muted-foreground">{spec.kind==="dots"?"요청 있는 구간만 표시 · 점에 올리면 구간별 표본 수 확인":featured?(hidden.length?"일부 호출을 숨긴 상태 · 범례로 전체 지표 표시":"실패는 전체 호출의 일부 · 막대 합계가 전체 호출 수"):"실제 관측 기록에서 집계 · 미확인 usage는 관측 없음"}</div></CardFooter>
  </Card>
}
const roots=new WeakMap<Element,Root>()
;(window as any).KUCharts={update(container:Element,points:Point[],hours="1",bucket="5"){let root=roots.get(container);if(!root){root=createRoot(container);roots.set(container,root)}root.render(<>{specs.map(spec=><MetricChart key={spec.id} spec={spec} points={points} hours={hours} bucket={bucket}/>)}</>)}}
