# Operations dashboard charts

The dashboard uses the official shadcn/ui `ChartContainer` and `ChartTooltipContent`
with Recharts v3, replacing the previous handwritten SVG renderer.
Source: https://ui.shadcn.com/r/styles/new-york-v4/chart.json
Docs: https://ui.shadcn.com/docs/components/chart

`dashboard-ui/src/components/ui/chart.tsx` retains the upstream component;
only the `cn` import points at the local utility. The upstream MIT license is
preserved in `dashboard-ui/SHADCN_LICENSE.md`. Bundled dependency license notices
are retained in the generated script.

## Build

From `serving/dashboard-ui`, run `npm ci` then `npm run build`. Commit both source,
lockfile and `src/academic_mcp/dashboard_assets` outputs. CI rebuilds and rejects
stale outputs. Python wheels and Docker images carry these prebuilt assets;
Node is not installed on EC2. The dashboard embeds its own JS/CSS, with no CDN,
external font or additional analytics service. Owner authentication, same-origin
CSP, loopback credential bridge and five-second polling are unchanged.

## Data behavior

Requests and API attempts use bars; cache rates and p50/p95 use lines; provider
reported tokens use an area. Null percentages/latencies/usage remain null and
`connectNulls=false` prevents bridging unknown buckets. Isolated observations
have visible dots. API stack totals sum embedding, rerank and generation attempts.
Interactive legends retain their state during polling and keep at least one series
visible. Tooltip timestamps and units use the source bucket and KST.
