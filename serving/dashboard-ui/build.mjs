import { build } from 'esbuild'
import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
process.chdir(fileURLToPath(new URL('.', import.meta.url)))
const out = '../src/academic_mcp/dashboard_assets'
mkdirSync(out, { recursive: true })
const built = await build({ entryPoints:['src/charts.tsx'], bundle:true, minify:true, format:'iife', target:['es2022'], jsx:'automatic', define:{'process.env.NODE_ENV':'"production"'}, write:false, legalComments:'eof' })
writeFileSync(`${out}/charts.js`, built.outputFiles[0].text.replace(/<\/script/gi,'<\\/script'))
execFileSync('node_modules/.bin/tailwindcss',['-i','src/styles.css','-o',`${out}/charts.css`,'--minify'],{stdio:'inherit'})
