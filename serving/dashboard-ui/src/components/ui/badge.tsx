import * as React from "react"
import { cn } from "../../lib/utils"
export function Badge({className,...props}:React.ComponentProps<"span">) {
 return <span data-slot="badge" className={cn("inline-flex w-fit shrink-0 items-center justify-center gap-1 rounded-md border border-border px-2 py-0.5 text-xs font-medium whitespace-nowrap",className)} {...props}/>
}
