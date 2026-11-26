import * as React from "react"
import { createRoot, type Root } from "react-dom/client"
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "./components/ui/chart"

type Point = { ts:number; requests:number; failures:number; cache_rate:number|null; effective_cache_rate:number|null; semantic_cache_rate:number|null; shared_rate:number|null; p50_ms:number|null; p95_ms:number|null; api_calls:Record<string,number>; generation_tokens:number|null }
type Series = { key:string; label:string; color:string }
type Spec = { id:string; title:string; description:string; kind:"bar"|"line"|"area"; unit:string; percent?:boolean; stacked?:boolean; series:Series[] }
const specs:Spec[] = [
  {id:"requests",title:"요청량과 실패",description:"각 시간 구간의 도구 호출 수",kind:"bar",unit:"회",series:[{key:"requests",label:"전체 호출",color:"var(--chart-2)"},{key:"failures",label:"오류·취소·시간 초과",color:"var(--chart-1)"}]},
  {id:"cache",title:"캐시와 요청 공유",description:"범례를 눌러 비교할 지표를 선택하세요",kind:"line",unit:"%",percent:true,series:[{key:"cache_rate",label:"응답 캐시",color:"var(--chart-4)"},{key:"effective_cache_rate",label:"검색 재사용 포함",color:"var(--chart-5)"},{key:"semantic_cache_rate",label:"의미 캐시",color:"var(--chart-1)"},{key:"shared_rate",label:"요청 공유",color:"var(--chart-2)"}]},
  {id:"latency",title:"서버 처리 시간",description:"전체 도구 호출의 p50과 p95, 외부 왕복 제외",kind:"line",unit:"초",series:[{key:"p50",label:"p50",color:"var(--chart-3)"},{key:"p95",label:"p95",color:"var(--chart-5)"}]},
  {id:"api",title:"실제 모델 API 호출",description:"임베딩, 리랭킹, 생성의 물리 시도 합계",kind:"bar",unit:"회",stacked:true,series:[{key:"embedding",label:"임베딩",color:"var(--chart-2)"},{key:"rerank",label:"리랭킹",color:"var(--chart-3)"},{key:"generation",label:"생성",color:"var(--chart-1)"}]},
  {id:"tokens",title:"생성 API가 보고한 총 토큰",description:"usage 미제공 구간은 연결하지 않습니다",kind:"area",unit:"토큰",series:[{key:"generation_tokens",label:"보고된 총 토큰",color:"var(--chart-4)"}]},
]
const stamp = (ts:number,long=false) => new Date(ts*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",...(long?{month:"2-digit",day:"2-digit"}:{}),hour:"2-digit",minute:"2-digit",hour12:false})
const number=(v:number,unit:string) => unit==="초"?v.toFixed(3):new Intl.NumberFormat("ko-KR",{maximumFractionDigits:1}).format(v)
function MetricChart({spec,points}:{spec:Spec;points:Point[]}) {
  const [hidden,setHidden]=React.useState<string[]>([])
  const data=React.useMemo(()=>points.map(p=>({...p,p50:p.p50_ms==null?null:p.p50_ms/1000,p95:p.p95_ms==null?null:p.p95_ms/1000,embedding:p.api_calls.embedding??0,rerank:p.api_calls.rerank??0,generation:p.api_calls.generation??0})),[points])
  const config=Object.fromEntries(spec.series.map(s=>[s.key,{label:s.label,color:s.color}])) satisfies ChartConfig
  const visible=spec.series.filter(s=>!hidden.includes(s.key))
  const long=points.length>1&&points.at(-1)!.ts-points[0].ts>43200
  const Chart=spec.kind==="bar"?BarChart:spec.kind==="area"?AreaChart:LineChart
  return <div className="chart" data-chart-kind={spec.kind} data-chart-library="shadcn-recharts">
    <div className="chart-heading"><div><h2 className="chart-title">{spec.title}</h2><p className="chart-description">{spec.description}</p></div></div>
    <div className="chart-series" aria-label={`${spec.title} 범례`}>{spec.series.map(s=><button type="button" key={s.key} aria-pressed={!hidden.includes(s.key)} onClick={()=>setHidden(h=>h.includes(s.key)?h.filter(k=>k!==s.key):h.length<spec.series.length-1?[...h,s.key]:h)}><i className="series-dot" style={{background:s.color}}/>{s.label}</button>)}</div>
    {!points.length?<div className="chart-empty">아직 관측된 데이터가 없습니다</div>:<ChartContainer id={`ops-${spec.id}`} config={config} className="chart-frame" aria-label={spec.title}>
      <Chart accessibilityLayer data={data} margin={{left:0,right:12,top:12,bottom:0}}>
        <CartesianGrid vertical={false} strokeDasharray="3 3" />
        <XAxis dataKey="ts" type="number" domain={["dataMin","dataMax"]} tickLine={false} axisLine={false} tickMargin={10} minTickGap={80} tickFormatter={v=>stamp(Number(v),long)} />
        <YAxis width={46} tickLine={false} axisLine={false} tickMargin={8} domain={spec.percent?[0,100]:[0,"auto"]} tickFormatter={v=>spec.percent?`${v}%`:Number(v)>=1000?`${Number(v)/1000}k`:String(Number(Number(v).toFixed(2)))} />
        <ChartTooltip cursor={spec.kind==="bar"?{fill:"var(--border)",fillOpacity:.3}:{stroke:"var(--muted-foreground)",strokeDasharray:"3 3"}} content={<ChartTooltipContent className="ops-tooltip" labelFormatter={(_label,payload)=>stamp(Number(payload?.[0]?.payload?.ts),true)} formatter={(value,name)=>{const item=config[String(name)];return <React.Fragment><i className="series-dot" style={{background:item?.color}}/><span className="text-muted-foreground">{item?.label??String(name)}</span><span className="ml-auto font-mono font-medium tabular-nums">{number(Number(value),spec.unit)} {spec.unit}</span></React.Fragment>}} />} />
        {visible.map(s=>spec.kind==="bar"?<Bar key={s.key} dataKey={s.key} fill={`var(--color-${s.key})`} radius={spec.stacked?[0,0,0,0]:[3,3,0,0]} stackId={spec.stacked?"api":undefined} maxBarSize={22} isAnimationActive={false}/>:spec.kind==="area"?<Area key={s.key} type="linear" dataKey={s.key} fill={`url(#fill-${spec.id})`} stroke={`var(--color-${s.key})`} strokeWidth={2} connectNulls={false} dot={{r:2,strokeWidth:0,fill:`var(--color-${s.key})`}} isAnimationActive={false}/>:<Line key={s.key} type="linear" dataKey={s.key} stroke={`var(--color-${s.key})`} strokeWidth={2} connectNulls={false} dot={{r:2,strokeWidth:0,fill:`var(--color-${s.key})`}} activeDot={{r:4}} isAnimationActive={false}/>)}
        {spec.kind==="area"&&<defs><linearGradient id={`fill-${spec.id}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={spec.series[0].color} stopOpacity={.3}/><stop offset="100%" stopColor={spec.series[0].color} stopOpacity={.02}/></linearGradient></defs>}
      </Chart>
    </ChartContainer>}
    <div className="chart-footer"><span>{points.length?`${stamp(points[0].ts,long)} ~ ${stamp(points.at(-1)!.ts,long)} KST`:"선택 기간"}</span><span>{spec.percent?"분모가 없는 구간은 공백":"60개 시간 구간 · 실측 서버 집계"}</span></div>
  </div>
}
const roots=new WeakMap<Element,Root>()
;(window as any).KUCharts={update(container:Element,points:Point[]){let root=roots.get(container);if(!root){root=createRoot(container);roots.set(container,root)}root.render(<>{specs.map(spec=><MetricChart key={spec.id} spec={spec} points={points}/>)}</>)}}
