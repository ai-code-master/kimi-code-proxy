#!/usr/bin/env python3
"""
Kimi Code OAuth Proxy

A local HTTP proxy that bridges OpenAI-compatible clients (like Hermes)
to the Kimi Code API (Anthropic Messages API format) with automatic
OAuth token refresh.

Usage:
    cp .env.example .env
    # edit .env with your settings
    python kimi_code_proxy.py
"""

import http.client
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ==================== Configuration ====================
def _env(key, default=""):
    return os.environ.get(key, default)

# Paths
CREDENTIALS_PATH = _env("KCP_CREDENTIALS_PATH", os.path.expanduser("~/.kimi/credentials/kimi-code.json"))
DEVICE_ID_PATH   = _env("KCP_DEVICE_ID_PATH",   os.path.expanduser("~/.kimi/device_id"))

# Endpoints
AUTH_ENDPOINT    = _env("KCP_AUTH_ENDPOINT",    "https://auth.kimi.com/api/oauth/token")
UPSTREAM_BASE    = _env("KCP_UPSTREAM_BASE",    "https://api.kimi.com/coding")
CLIENT_ID        = _env("KCP_CLIENT_ID",        "")

# Server
PROXY_HOST       = _env("KCP_HOST", "127.0.0.1")
PROXY_PORT       = int(_env("KCP_PORT", "8765"))
MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "1"))
MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))

# Logging
LOG_DIR          = _env("KCP_LOG_DIR", os.path.expanduser("~/.hermes/logs"))
LOG_FILE         = os.path.join(LOG_DIR, "kimi-proxy.log")
LOG_MAX_BYTES    = int(_env("KCP_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(_env("KCP_LOG_BACKUP_COUNT", "3"))

# Device info (override to avoid leaking real machine names)
DEVICE_NAME      = _env("KCP_DEVICE_NAME",      "KimiProxy")
DEVICE_MODEL     = _env("KCP_DEVICE_MODEL",     "Desktop")
DEVICE_PLATFORM  = _env("KCP_DEVICE_PLATFORM",  "macOS")
DEVICE_VERSION   = _env("KCP_DEVICE_VERSION",   "2.1.153")

# ==================== Validation ====================
if not CLIENT_ID:
    print("ERROR: KCP_CLIENT_ID is required. Set it in your .env file.", file=sys.stderr)
    sys.exit(1)

# ==================== Logging ====================
os.makedirs(LOG_DIR, exist_ok=True)

class RotatingLogHandler(logging.Handler):
    def __init__(self, filename, max_bytes, backup_count):
        super().__init__()
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.stream = None
        self._open()

    def _open(self):
        if self.stream:
            self.stream.close()
        self.stream = open(self.filename, "a", encoding="utf-8")

    def _rotate(self):
        if os.path.exists(self.filename) and os.path.getsize(self.filename) >= self.max_bytes:
            self.stream.close()
            for i in range(self.backup_count - 1, 0, -1):
                src, dst = f"{self.filename}.{i}", f"{self.filename}.{i+1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self.filename):
                os.replace(self.filename, f"{self.filename}.1")
            self._open()

    def emit(self, record):
        try:
            self._rotate()
            self.stream.write(self.format(record) + "\n")
            self.stream.flush()
        except Exception:
            pass

handler = RotatingLogHandler(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger = logging.getLogger("kimi-proxy")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# ==================== Concurrency Control ====================
kimi_semaphore = threading.Semaphore(MAX_CONCURRENT)

# ==================== Token Management ====================
class TokenManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._refresh_cond = threading.Condition(self._refresh_lock)
        self._data = {}
        self._device_id = self._load_device_id()
        self._device_info = self._load_device_info()
        self._load()

    def _load_device_id(self):
        try:
            with open(DEVICE_ID_PATH) as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    def _load_device_info(self):
        import platform
        return {
            "platform": DEVICE_PLATFORM,
            "version": DEVICE_VERSION,
            "device_name": DEVICE_NAME,
            "device_model": DEVICE_MODEL,
            "os_version": platform.mac_ver()[0] or _env("KCP_OS_VERSION", "15.0"),
            "device_id": self._device_id,
        }

    def _load(self):
        try:
            with open(CREDENTIALS_PATH) as f:
                self._data = json.load(f)
            remaining = max(0, self._data.get("expires_at", 0) - time.time())
            logger.info(f"Token loaded, remaining {remaining:.0f}s")
        except Exception as e:
            logger.error(f"Load token failed: {e}")
            self._data = {}

    def _save(self):
        try:
            with open(CREDENTIALS_PATH, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.error(f"Save token failed: {e}")

    def get_token(self):
        with self._lock:
            return self._data.get("access_token", "")

    def get_headers(self):
        d = self._device_info
        return {
            "X-Msh-Platform": d["platform"],
            "X-Msh-Version": d["version"],
            "X-Msh-Device-Name": d["device_name"],
            "X-Msh-Device-Model": d["device_model"],
            "X-Msh-Os-Version": d["os_version"],
            "X-Msh-Device-Id": d["device_id"],
        }

    def should_refresh(self):
        with self._lock:
            return (self._data.get("expires_at", 0) - time.time()) < REFRESH_THRESHOLD

    def refresh(self):
        with self._refresh_lock:
            if self._refreshing:
                logger.info("Waiting for refresh...")
                self._refresh_cond.wait(timeout=30)
                return True
            self._refreshing = True
        try:
            with self._lock:
                refresh_token = self._data.get("refresh_token", "")
                if not refresh_token:
                    return False
                d = self._device_info
                conn = http.client.HTTPSConnection("auth.kimi.com", timeout=30)
                try:
                    body = urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": refresh_token,
                    })
                    conn.request(
                        "POST",
                        "/api/oauth/token",
                        body=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    resp = conn.getresponse()
                    new_tokens = json.loads(resp.read())
                finally:
                    conn.close()
                self._data["access_token"] = new_tokens["access_token"]
                self._data["refresh_token"] = new_tokens["refresh_token"]
                self._data["token_type"] = new_tokens.get("token_type", "Bearer")
                self._data["expires_in"] = new_tokens.get("expires_in", 900)
                self._data["expires_at"] = time.time() + new_tokens.get("expires_in", 900)
                self._save()
                logger.info("Token refresh OK")
                return True
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return False
        finally:
            with self._refresh_lock:
                self._refreshing = False
                self._refresh_cond.notify_all()

token_mgr = TokenManager()

# ==================== Background Refresh Thread ====================
def refresh_worker():
    while True:
        time.sleep(REFRESH_INTERVAL)
        if token_mgr.should_refresh():
            logger.info("Token expiring, refreshing...")
            token_mgr.refresh()

threading.Thread(target=refresh_worker, daemon=True).start()

# ==================== Core Request Function ====================
def _do_kimi_request(method, target_url, body, headers, retries=0):
    parsed = urllib.parse.urlparse(target_url)
    host, path = parsed.netloc, parsed.path + ("?" + parsed.query if parsed.query else "")
    conn = http.client.HTTPSConnection(host, timeout=120)
    try:
        req_headers = dict(headers)
        req_headers["Connection"] = "close"
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        status = resp.status
        if status in (502, 429, 503) and retries < MAX_RETRIES:
            wait = BACKOFF_BASE * (2 ** retries)
            logger.warning(f"HTTP {status}, retry in {wait:.1f}s (attempt {retries+1})...")
            time.sleep(wait)
            conn.close()
            return _do_kimi_request(method, target_url, body, headers, retries + 1)
        resp._conn = conn
        return resp, None
    except Exception as e:
        conn.close()
        return None, e

# ==================== HTTP Proxy ====================
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        if self.path != "/healthz":
            logger.info(f"{self.command} {self.path} -> {args[1]}")

    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def _forward(self, method):
        token = token_mgr.get_token()
        if not token:
            self._send_error(401, "No Kimi Code token available")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        path = self.path
        if path.startswith("/api/"):
            path = path[4:]
        if path.startswith("/v1/models/"):
            path = "/v1/models"

        if path.startswith("/v1/"):
            target_url = f"{UPSTREAM_BASE}{path}"
        elif path == "/chat/completions":
            target_url = f"{UPSTREAM_BASE}/v1/chat/completions"
        elif path == "/models":
            target_url = f"{UPSTREAM_BASE}/v1/models"
        else:
            target_url = f"{UPSTREAM_BASE}/v1{path}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "KimiCLI/1.5",
            "Accept": "application/json",
        }
        headers.update(token_mgr.get_headers())
        for key in ("openai-beta", "anthropic-version", "x-request-id"):
            if key in self.headers:
                headers[key] = self.headers[key]

        acquired = False
        try:
            acquired = kimi_semaphore.acquire(timeout=120)
            if not acquired:
                self._send_error(503, "Kimi API concurrency limit exceeded")
                return

            logger.info(f"Forwarding {method} {path} (semaphore acquired)")
            resp, error = _do_kimi_request(method, target_url, body, headers)

            if resp and resp.status == 401:
                logger.warning("Got 401, refreshing token and retrying...")
                if token_mgr.refresh():
                    new_token = token_mgr.get_token()
                    new_headers = {**headers, "Authorization": f"Bearer {new_token}"}
                    resp, error = _do_kimi_request(method, target_url, body, new_headers)
                    if resp and resp.status != 401:
                        logger.info("Retry after refresh OK")
                    else:
                        logger.error("Retry still failed")
                else:
                    logger.error("Refresh failed")

            if resp:
                self._send_response(resp)
            else:
                logger.error(f"Forward error: {error}")
                self._send_error(502, str(error))
        finally:
            if acquired:
                kimi_semaphore.release()

    def _send_response(self, resp):
        try:
            data = resp.read()
        except Exception as e:
            logger.error(f"读取响应体失败: {e}")
            self._send_error(502, f"Failed to read upstream response: {e}")
            resp.close()
            if hasattr(resp, "_conn"):
                resp._conn.close()
            return

        self.send_response(resp.status)
        for header, value in resp.getheaders():
            if header.lower() in ("connection", "transfer-encoding"):
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected")
        finally:
            resp.close()
            if hasattr(resp, "_conn"):
                resp._conn.close()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            health = {
                "status": "ok",
                "version": "2.3",
                "token_expires_at": token_mgr._data.get("expires_at", 0),
                "token_remaining": max(0, token_mgr._data.get("expires_at", 0) - time.time()),
                "concurrent_limit": MAX_CONCURRENT,
                "concurrent_active": MAX_CONCURRENT - kimi_semaphore._value,
                "features": [
                    "connection_close",
                    "serial_upstream",
                    "backoff_retry",
                    "auto_refresh_401",
                    "full_xmsh_headers",
                ],
            }
            self.wfile.write(json.dumps(health).encode())
            return
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def main():
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    logger.info("Kimi Code Proxy started")
    logger.info(f"  Listen: http://{PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"  Upstream: {UPSTREAM_BASE}")
    logger.info(f"  Serial: MAX_CONCURRENT={MAX_CONCURRENT}, Connection: close")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
