import * as React from "react"
import { cn } from "../../lib/utils"
export function Alert({className,...props}:React.ComponentProps<"div">) {return <div data-slot="alert" role="note" className={cn("relative grid w-full grid-cols-[0_1fr] items-start gap-y-0.5 rounded-lg border border-border bg-card px-4 py-3 text-sm text-card-foreground has-[>svg]:grid-cols-[16px_1fr] has-[>svg]:gap-x-3 [&>svg]:size-4 [&>svg]:translate-y-0.5",className)} {...props}/>}
export function AlertDescription({className,...props}:React.ComponentProps<"div">) {return <div data-slot="alert-description" className={cn("col-start-2 grid gap-1 text-sm leading-relaxed text-muted-foreground",className)} {...props}/>}
