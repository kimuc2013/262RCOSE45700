import * as React from "react"
import { createRoot, type Root } from "react-dom/client"
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"
import { ChartContainer, ChartLegend, ChartTooltip, ChartTooltipContent, type ChartConfig } from "./components/ui/chart"

import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "./components/ui/card"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./components/ui/select"

type Point = { ts:number; requests:number; failures:number; cache_rate:number|null; effective_cache_rate:number|null; semantic_cache_rate:number|null; shared_rate:number|null; p50_ms:number|null; p95_ms:number|null; api_calls:Record<string,number>; generation_tokens:number|null }
type Series = { key:string; label:string; color:string }
type Spec = { id:string; title:string; description:string; kind:"bar"|"line"|"area"; unit:string; percent?:boolean; stacked?:boolean; series:Series[] }
const specs:Spec[] = [
  {id:"requests",title:"요청량과 실패",description:"선택 기간의 전체 도구 호출과 실패",kind:"area",unit:"회",series:[{key:"requests",label:"전체 호출",color:"var(--chart-1)"},{key:"failures",label:"오류·취소·시간 초과",color:"var(--chart-2)"}]},
  {id:"cache",title:"캐시와 요청 공유",description:"응답 캐시, 검색 재사용과 진행 중 요청 공유",kind:"line",unit:"%",percent:true,series:[{key:"cache_rate",label:"응답 캐시",color:"var(--chart-1)"},{key:"effective_cache_rate",label:"검색 재사용 포함",color:"var(--chart-2)"},{key:"semantic_cache_rate",label:"의미 캐시",color:"var(--chart-3)"},{key:"shared_rate",label:"요청 공유",color:"var(--chart-4)"}]},
  {id:"latency",title:"서버 처리 시간",description:"전체 도구 호출의 p50과 p95, 외부 왕복 제외",kind:"line",unit:"초",series:[{key:"p50",label:"p50",color:"var(--chart-1)"},{key:"p95",label:"p95",color:"var(--chart-2)"}]},
  {id:"api",title:"실제 모델 API 호출",description:"임베딩, 리랭킹, 생성의 물리 시도 합계",kind:"bar",unit:"회",stacked:true,series:[{key:"embedding",label:"임베딩",color:"var(--chart-1)"},{key:"rerank",label:"리랭킹",color:"var(--chart-2)"},{key:"generation",label:"생성",color:"var(--chart-3)"}]},
  {id:"tokens",title:"생성 API가 보고한 총 토큰",description:"usage 미제공 구간은 연결하지 않습니다",kind:"area",unit:"토큰",series:[{key:"generation_tokens",label:"보고된 총 토큰",color:"var(--chart-1)"}]},
]
const stamp = (ts:number,long=false) => new Date(ts*1000).toLocaleString("ko-KR",{timeZone:"Asia/Seoul",...(long?{month:"2-digit",day:"2-digit"}:{}),hour:"2-digit",minute:"2-digit",hour12:false})
const number=(v:number,unit:string) => unit==="초"?v.toFixed(3):new Intl.NumberFormat("ko-KR",{maximumFractionDigits:1}).format(v)
function MetricChart({spec,points,hours}:{spec:Spec;points:Point[];hours:string}) {
  const [hidden,setHidden]=React.useState<string[]>([])
  const data=React.useMemo(()=>points.map(p=>({...p,p50:p.p50_ms==null?null:p.p50_ms/1000,p95:p.p95_ms==null?null:p.p95_ms/1000,embedding:p.api_calls.embedding??0,rerank:p.api_calls.rerank??0,generation:p.api_calls.generation??0})),[points])
  const config=Object.fromEntries(spec.series.map(s=>[s.key,{label:s.label,color:s.color}])) satisfies ChartConfig
  const visible=spec.series.filter(s=>!hidden.includes(s.key))
  const long=points.length>1&&points.at(-1)!.ts-points[0].ts>43200
  const Chart=spec.kind==="bar"?BarChart:spec.kind==="area"?AreaChart:LineChart
  const featured=spec.id==="requests"
  function setRange(value:string) {
    const control=document.getElementById("hours") as HTMLSelectElement
    control.value=value
    control.dispatchEvent(new Event("change",{bubbles:true}))
  }
  function isolatedDot(key:string) {
    return (props:any) => {
      const i=props.index, before=i>0?(data[i-1] as any)[key]:null, after=i+1<data.length?(data[i+1] as any)[key]:null
      return before==null&&after==null&&props.value!=null?<circle key={`${key}-${i}`} cx={props.cx} cy={props.cy} r={2.5} fill={`var(--color-${key})`} stroke="none"/>:<g key={`${key}-${i}`}/>
    }
  }
  return <Card data-chart-kind={spec.kind} data-chart-library="shadcn-recharts" data-featured={featured} className={featured?"pt-0":""}>
    <CardHeader className={featured?"flex items-center gap-2 space-y-0 border-b py-5 sm:flex-row":""}>
      <div className="grid flex-1 gap-1"><CardTitle>{spec.title}</CardTitle><CardDescription>{spec.description}</CardDescription></div>
      {featured&&<Select value={hours} onValueChange={setRange}><SelectTrigger className="w-[160px] rounded-lg sm:ml-auto" aria-label="차트 조회 기간"><SelectValue/></SelectTrigger><SelectContent className="rounded-xl"><SelectItem value=".25">최근 15분</SelectItem><SelectItem value="1">최근 1시간</SelectItem><SelectItem value="24">최근 24시간</SelectItem></SelectContent></Select>}
    </CardHeader>
    <CardContent className={featured?"px-2 pt-4 sm:px-6 sm:pt-6":""}>
      {!points.length?<div className="chart-empty">아직 관측된 데이터가 없습니다</div>:<ChartContainer id={`ops-${spec.id}`} config={config} className="aspect-auto h-[250px] w-full" aria-label={spec.title}>
        <Chart accessibilityLayer data={data} margin={{left:12,right:12,top:4,bottom:0}}>
          <CartesianGrid vertical={false}/>
          <YAxis hide domain={spec.percent?[0,100]:[0,"auto"]}/>
          <XAxis dataKey="ts" type="number" domain={["dataMin","dataMax"]} tickLine={false} axisLine={false} tickMargin={8} minTickGap={32} tickFormatter={v=>stamp(Number(v),long)}/>
          <ChartTooltip cursor={false} content={<ChartTooltipContent labelFormatter={(_label,payload)=>stamp(Number(payload?.[0]?.payload?.ts),true)} indicator="dot" formatter={(value,name)=>{const item=config[String(name)];return <><div className="h-2.5 w-2.5 shrink-0 rounded-[2px]" style={{background:item?.color}}/><span className="text-muted-foreground">{item?.label??String(name)}</span><span className="ml-auto font-mono font-medium tabular-nums">{number(Number(value),spec.unit)} {spec.unit}</span></>}}/>}/>
          <ChartLegend content={()=><div className="flex flex-wrap items-center justify-center gap-4 pt-3" aria-label={`${spec.title} 범례`}>{spec.series.map(s=><button type="button" key={s.key} className="chart-legend-item" aria-pressed={!hidden.includes(s.key)} onClick={()=>setHidden(h=>h.includes(s.key)?h.filter(k=>k!==s.key):h.length<spec.series.length-1?[...h,s.key]:h)}><div className="h-2 w-2 shrink-0 rounded-[2px]" style={{background:s.color}}/>{s.label}</button>)}</div>}/>
          {visible.map(s=>spec.kind==="bar"?<Bar key={s.key} dataKey={s.key} fill={`var(--color-${s.key})`} radius={spec.stacked?[0,0,4,4]:4} stackId={spec.stacked?"api":undefined} maxBarSize={26} isAnimationActive={false}/>:spec.kind==="area"?<Area key={s.key} type="monotone" dataKey={s.key} fill={`url(#fill-${spec.id}-${s.key})`} stroke={`var(--color-${s.key})`} strokeWidth={1} connectNulls={false} dot={isolatedDot(s.key)} isAnimationActive={false}/>:<Line key={s.key} type="monotone" dataKey={s.key} stroke={`var(--color-${s.key})`} strokeWidth={2} connectNulls={false} dot={isolatedDot(s.key)} activeDot={{r:4}} isAnimationActive={false}/>)}
          <defs>{spec.series.map(s=><linearGradient key={s.key} id={`fill-${spec.id}-${s.key}`} x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor={s.color} stopOpacity={.8}/><stop offset="95%" stopColor={s.color} stopOpacity={.1}/></linearGradient>)}</defs>
        </Chart>
      </ChartContainer>}
    </CardContent>
    {!featured&&<CardFooter className="flex-col items-start gap-2 text-sm"><div className="font-medium leading-none">{points.length?`${stamp(points[0].ts,long)} ~ ${stamp(points.at(-1)!.ts,long)} KST`:"선택 기간"}</div><div className="leading-none text-muted-foreground">{spec.percent?"범례로 지표 선택, 분모가 없는 구간은 공백":`시간 구간별 실측값 (${spec.unit}), 미확인 구간은 연결하지 않음`}</div></CardFooter>}
  </Card>
}
const roots=new WeakMap<Element,Root>()
;(window as any).KUCharts={update(container:Element,points:Point[],hours="1"){let root=roots.get(container);if(!root){root=createRoot(container);roots.set(container,root)}root.render(<>{specs.map(spec=><MetricChart key={spec.id} spec={spec} points={points} hours={hours}/>)}</>)}}
