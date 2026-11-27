import * as React from "react"
import { createRoot, type Root } from "react-dom/client"
import { Pause, Play, RefreshCw, Info, Circle } from "lucide-react"
import { Button } from "./components/ui/button"
import { Badge } from "./components/ui/badge"
import { Alert, AlertDescription } from "./components/ui/alert"
import { Input } from "./components/ui/input"
import { Card, CardHeader, CardDescription, CardContent, CardFooter } from "./components/ui/card"
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./components/ui/select"
const roots=new WeakMap<Element,Root>()
function render(id:string,content:React.ReactNode){const el=document.getElementById(id)!;let root=roots.get(el);if(!root){root=createRoot(el);roots.set(el,root)}root.render(content)}
const control=(id:string)=>document.getElementById(id) as HTMLInputElement
function change(id:string,value:string){control(id).value=value;control(id).dispatchEvent(new Event("change",{bubbles:true}))}
const options:Record<string,[string,string][]>= {hours:[["0.25","최근 15분"],["1","최근 1시간"],["24","최근 24시간"]],bucket_minutes:[["1","1분 집계"],["5","5분 집계"],["15","15분 집계"]],traffic:[["all","전체 호출"],["live","일반 호출 (실험 표식 없음)"],["experiment","통제 실험"]]}
function Controls({paused}:{paused:boolean}) {return <><div className="grid gap-2"><Badge>TEAM 7 / LIVE OPERATIONS</Badge><h1 className="text-2xl font-semibold tracking-tight">학사정보 MCP 운영 현황</h1><p className="m-0 text-sm text-muted-foreground">5초 자동 갱신 · 서버 집계 · 개인정보 본문 미수집</p></div><div className="flex flex-wrap items-center gap-2" data-console-controls>{Object.entries(options).map(([id,items])=><Select key={id} value={control(id).value} onValueChange={v=>change(id,v)}><SelectTrigger aria-label={{hours:"조회 기간",bucket_minutes:"집계 간격",traffic:"트래픽 구분"}[id]} className={id==="traffic"?"w-[228px]":"w-[136px]"}><SelectValue/></SelectTrigger><SelectContent position="popper">{items.map(([value,label])=><SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select>)}<Button aria-pressed={paused} onClick={()=>control("pause").click()}>{paused?<Play/>:<Pause/>}{paused?"자동 갱신 재개":"일시정지"}</Button><Button onClick={()=>control("refresh").click()}><RefreshCw/>지금 갱신</Button></div></>}
function Login(){const input=React.useRef<HTMLInputElement>(null);function connect(){control("key").value=input.current?.value??"";if(input.current)input.current.value="";control("connect").click()}return <Card className="my-4"><CardHeader><CardDescription><label htmlFor="owner-key">관리자 Bearer 키</label></CardDescription></CardHeader><CardContent className="flex flex-wrap gap-2"><Input id="owner-key" ref={input} type="password" autoComplete="off" placeholder="팀 테스트 키로는 접근할 수 없습니다" className="max-w-md" onKeyDown={e=>{if(e.key==="Enter")connect()}}/><Button onClick={connect}>연결</Button></CardContent><CardFooter className="text-xs text-muted-foreground">키는 이 탭의 메모리에만 유지됩니다.</CardFooter></Card>}
;(window as any).KUConsole={
 sync(paused=false){render("console-header",<Controls paused={paused}/>)},
 init(local:boolean){this.sync();if(!local)render("console-login",<Login/>);render("console-notice",<Alert><Info/><AlertDescription>시간은 서버 도구 실행 기준입니다. 클라이언트 연결·외부 왕복·추가 LLM 작성은 제외됩니다. answered는 정답 판정이 아닙니다. 일반 호출에는 배포 점검도 포함될 수 있습니다.</AlertDescription></Alert>)},
 status(text:string,kind:string){render("status",<><Badge className={kind==="good"?"text-emerald-300":kind==="bad"?"text-rose-300":"text-muted-foreground"}><Circle className="size-2 fill-current"/>{kind==="good"?"연결됨":kind==="bad"?"연결 오류":"대기 / 정지"}</Badge><span className="text-xs text-muted-foreground">{text.replace(/^● 연결됨 \/ /,"")}</span></>)},
 cards(cards:{label:string;value:string;note:string}[]){render("cards",<>{cards.map(c=><Card key={c.label} data-summary-card><CardHeader><CardDescription>{c.label}</CardDescription></CardHeader><CardContent><div className="text-3xl font-semibold tracking-tight tabular-nums">{c.value}</div></CardContent><CardFooter className="text-xs leading-relaxed text-muted-foreground">{c.note}</CardFooter></Card>)}</>)}
}
