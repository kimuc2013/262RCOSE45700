import * as React from "react"
import { cn } from "../../lib/utils"
export function Button({className,variant="outline",...props}:React.ComponentProps<"button">&{variant?:"outline"|"default"}) {
 return <button data-slot="button" type="button" className={cn("inline-flex h-9 shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-md px-4 py-2 text-sm font-medium transition-all outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4",variant==="outline"?"border border-input bg-background hover:bg-accent hover:text-accent-foreground":"bg-primary text-primary-foreground hover:bg-primary/90",className)} {...props}/>
}
