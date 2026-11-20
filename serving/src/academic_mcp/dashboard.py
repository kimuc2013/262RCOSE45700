"""Loopback-only live dashboard bridge. Owner credential never enters the browser."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import threading

import httpx
from .live_metrics import dashboard_html, HEADERS


def make_handler(client, upstream, token, port):
    slots = threading.BoundedSemaphore(2)
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type='application/json'):
            self.send_response(status)
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            host = self.headers.get('Host')
            origin = self.headers.get('Origin')
            if host not in hosts or (origin and origin not in {'http://'+h for h in hosts}):
                return self.reply(403, b'{"error":"invalid_origin"}')
            url = urlsplit(self.path)
            if url.path == '/':
                return self.reply(200, dashboard_html(local=True).encode(), 'text/html; charset=utf-8')
            if url.path != '/api/metrics':
                return self.reply(404, b'{"error":"not_found"}')
            query = parse_qs(url.query)
            hours = query.get('hours', ['1'])[0]
            if hours not in ('.25', '0.25', '1', '24'):
                return self.reply(400, b'{"error":"invalid_window"}')
            if not slots.acquire(blocking=False):
                return self.reply(429, b'{"error":"busy"}')
            try:
                response = client.get(upstream+'/ops/api/metrics', params={'hours': hours},
                    headers={'Authorization': 'Bearer '+token}, timeout=10, follow_redirects=False)
                if response.status_code != 200:
                    return self.reply(response.status_code if response.status_code in (401, 503) else 502,
                                      b'{"error":"upstream_unavailable"}')
                data = response.json()
                return self.reply(200, json.dumps(data, ensure_ascii=False).encode())
            except (httpx.HTTPError, ValueError):
                return self.reply(502, b'{"error":"upstream_unavailable"}')
            finally:
                slots.release()
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18711)
    args = parser.parse_args()
    token = os.getenv('MCP_OWNER_TOKEN') or os.getenv('MCP_DEMO_TOKEN')
    url = os.getenv('MCP_URL', '')
    parsed = urlsplit(url)
    if not token or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        parser.error('Set HTTPS MCP_URL and private MCP_OWNER_TOKEN (or MCP_DEMO_TOKEN)')
    if not 1024 <= args.port <= 65535:
        parser.error('Invalid port')
    upstream = parsed.scheme+'://'+parsed.netloc
    with httpx.Client() as client:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(client, upstream, token, args.port))
        server.daemon_threads = True
        print(f'Live dashboard: http://127.0.0.1:{args.port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
