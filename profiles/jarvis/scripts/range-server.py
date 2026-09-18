#!/usr/bin/env python3
"""HTTP server with Range support for DGX backup push. Serves a single directory."""
import os, sys, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head=True)

    def _serve(self, head=False):
        root = self.server.root
        path = unquote(self.path.split('?', 1)[0])
        if path == '/':
            if head:
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
            else:
                self.send_dir_listing(root)
            return
        fpath = os.path.normpath(os.path.join(root, path.lstrip('/')))
        if not fpath.startswith(os.path.normpath(root)):
            self.send_error(403); return
        if not os.path.isfile(fpath):
            self.send_error(404); return
        size = os.path.getsize(fpath)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng and rng.startswith('bytes='):
            try:
                spec = rng[6:].split('-')[0]
                start = int(spec)
                end = size - 1
            except ValueError:
                pass
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if rng:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head:
            return
        with open(fpath, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk: break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def send_dir_listing(self, root):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        entries = []
        for name in os.listdir(root):
            fp = os.path.join(root, name)
            if os.path.isfile(fp):
                entries.append((name, os.path.getsize(fp)))
        entries.sort()
        html = ['<html><body><h1>DGX Backup</h1><ul>']
        for name, sz in entries:
            html.append(f'<li><a href="/{name}">{name}</a> ({sz} bytes)</li>')
        html.append('</ul></body></html>')
        self.wfile.write(''.join(html).encode())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--port', type=int, default=18888)
    ap.add_argument('--bind', default='0.0.0.0')
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), RangeHandler)
    server.root = args.root
    print(f"Serving {args.root} on {args.bind}:{args.port} (range-support)", flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
