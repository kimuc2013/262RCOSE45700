"""Owner-gated metrics routes. Public edge authenticates, private backend verifies its marker."""
import asyncio
from pathlib import Path
import sqlite3
import time

from starlette.responses import HTMLResponse, JSONResponse
from .metrics import read_report

HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'none'"}


def dashboard_html(local=False):
    text = Path(__file__).with_name('live_dashboard.html').read_text()
    return text.replace('__LOCAL_MODE__', 'true' if local else 'false')


class MetricsReader:
    def __init__(self, path, *, ttl=5):
        self.path, self.ttl = path, ttl
        self.cache = {}
        self.lock = asyncio.Lock()

    async def get(self, hours):
        async with self.lock:
            cached = self.cache.get(hours)
            if cached and time.monotonic()-cached[0] < self.ttl:
                return cached[1]
            # Analytics does not occupy MCP request slots or block its event loop.
            report = await asyncio.to_thread(read_report, self.path, hours, include_series=True)
            self.cache[hours] = (time.monotonic(), report)
            return report


def register_routes(mcp, path):
    reader = MetricsReader(path)

    @mcp.custom_route('/ops', methods=['GET'])
    @mcp.custom_route('/ops/', methods=['GET'])
    async def page(request):
        # This shell contains no metrics or credentials. API requires owner auth.
        return HTMLResponse(dashboard_html(), headers=HEADERS)

    @mcp.custom_route('/ops/api/metrics', methods=['GET'])
    async def metrics(request):
        # The backend is loopback/private Docker network only. Caddy strips this
        # header on other routes and sets it only AFTER matching the owner key.
        if request.headers.get('x-ku-metrics-access') != 'owner':
            return JSONResponse({'error': 'unauthorized'}, status_code=401, headers=HEADERS)
        try:
            hours = float(request.query_params.get('hours', '1'))
        except ValueError:
            hours = 0
        if hours not in (.25, 1, 24):
            return JSONResponse({'error': 'invalid_window'}, status_code=400, headers=HEADERS)
        try:
            report = await reader.get(hours)
        except (sqlite3.Error, OSError):
            return JSONResponse({'error': 'metrics_not_ready'}, status_code=503, headers=HEADERS)
        return JSONResponse(report, headers=HEADERS)
