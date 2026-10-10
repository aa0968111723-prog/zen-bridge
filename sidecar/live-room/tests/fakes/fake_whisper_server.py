"""Fake whisper-server for tests/test_asr_gpu.py. Mode via FAKE_WS_MODE: ok | crash | hang | vkerr | nohealth."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
mode = os.environ.get("FAKE_WS_MODE", "ok")
log = os.environ.get("FAKE_WS_ARGV")
if log:
    with open(log, "w", encoding="utf-8") as f:
        json.dump(args, f)
sys.stderr.write("ggml_vulkan: Found 1 Vulkan devices:\nggml_vulkan: 0 = Fake Radeon (fake driver) | uma: 1 | fp16: 1\n")
sys.stderr.flush()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health" and mode != "nohealth":
            return self._send(200, {"status": "ok"})
        return self._send(503, {"status": "loading"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        if mode == "crash":
            os._exit(3)
        if mode == "hang":
            time.sleep(60)
        if mode == "vkerr":
            sys.stderr.write("ggml_vulkan: vk::Queue::submit: ErrorDeviceLost VK_ERROR_DEVICE_LOST\n")
            sys.stderr.flush()
        return self._send(200, {"text": "測試字幕"})


HTTPServer(("127.0.0.1", port), H).serve_forever()
