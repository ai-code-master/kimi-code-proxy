#!/usr/bin/env python3
"""
Kimi Code OAuth Proxy v2.7

A local HTTP proxy that bridges OpenAI-compatible clients (like Hermes)
to the Kimi Code API with automatic OAuth token refresh.

Changelog v2.7:
- Graceful shutdown on SIGTERM/SIGINT (waits for active requests)
- Upstream health probe in /healthz (cached, background-checked every 30s)
- KCP_DEBUG_BODY switch for request/response body logging
- Body parse errors logged at debug level instead of silently swallowed
"""

import http.client
import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ==================== Configuration ====================
def _env(key, default=""):
    return os.environ.get(key, default)


def _load_dotenv(path=".env"):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k not in os.environ:
                        os.environ[k] = v.strip()
    except Exception:
        pass


# Auto-load .env from the same directory as this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(_script_dir, ".env"))

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
MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "2"))
MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))
UPSTREAM_TIMEOUT = int(_env("KCP_UPSTREAM_TIMEOUT", "600"))
QUEUE_TIMEOUT    = int(_env("KCP_QUEUE_TIMEOUT", "300"))
MAX_BODY_SIZE    = int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024)))  # 50MB
SLOW_REQUEST_THRESHOLD = float(_env("KCP_SLOW_REQUEST_THRESHOLD", "30.0"))
GRACEFUL_SHUTDOWN_WAIT = int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30"))
DEBUG_BODY       = _env("KCP_DEBUG_BODY", "").lower() in ("1", "true", "yes")

# Logging
LOG_DIR          = _env("KCP_LOG_DIR", os.path.expanduser("~/.hermes/logs"))
LOG_FILE         = os.path.join(LOG_DIR, "kimi-proxy.log")
LOG_MAX_BYTES    = int(_env("KCP_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(_env("KCP_LOG_BACKUP_COUNT", "3"))
LOG_DIR_MAX_BYTES = int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024)))

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


def _ensure_disk_space():
    """If log dir is over limit, delete oldest backup files."""
    try:
        total = 0
        files = []
        for entry in os.listdir(LOG_DIR):
            path = os.path.join(LOG_DIR, entry)
            if os.path.isfile(path) and "kimi-proxy" in entry:
                s = os.path.getsize(path)
                total += s
                files.append((path, os.path.getmtime(path), s))
        if total > LOG_DIR_MAX_BYTES:
            files.sort(key=lambda x: x[1])  # oldest first
            for path, mtime, s in files:
                if total <= LOG_DIR_MAX_BYTES * 0.8:
                    break
                try:
                    os.remove(path)
                    total -= s
                    print(f"[kimi-proxy] Disk guard: removed old log {path}", file=sys.stderr)
                except Exception:
                    pass
    except Exception:
        pass


class RotatingLogHandler(logging.Handler):
    def __init__(self, filename, max_bytes, backup_count):
        super().__init__()
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.stream = None
        self._lock = threading.Lock()
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
            with self._lock:
                _ensure_disk_space()
                self._rotate()
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
        except Exception:
            pass


_handler = RotatingLogHandler(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger = logging.getLogger("kimi-proxy")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)

# Also capture uncaught exceptions from threads into our log
def _log_uncaught(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception: %s", exc_value, exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_uncaught

# ==================== Metrics ====================
class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.retry_count = 0
        self.client_reset_count = 0
        self.slow_request_count = 0
        self.latency_sum = 0.0
        self.latency_count = 0
        self.status_counts = {}
        self.queue_wait_sum = 0.0
        self.queue_wait_count = 0
        self.model_counts = {}

    def record_request(self, latency: float, status: int | None = None, queue_wait: float = 0.0, model: str = ""):
        with self._lock:
            self.request_count += 1
            self.latency_sum += latency
            self.latency_count += 1
            if queue_wait > 0:
                self.queue_wait_sum += queue_wait
                self.queue_wait_count += 1
            if status is not None:
                self.status_counts[status] = self.status_counts.get(status, 0) + 1
                if status >= 500 or status == 429:
                    self.error_count += 1
            if model:
                self.model_counts[model] = self.model_counts.get(model, 0) + 1
            if latency > SLOW_REQUEST_THRESHOLD:
                self.slow_request_count += 1

    def record_timeout(self):
        with self._lock:
            self.timeout_count += 1
            self.error_count += 1

    def record_retry(self):
        with self._lock:
            self.retry_count += 1

    def record_client_reset(self):
        with self._lock:
            self.client_reset_count += 1

    def snapshot(self):
        with self._lock:
            avg = (self.latency_sum / self.latency_count) if self.latency_count else 0.0
            avg_queue = (self.queue_wait_sum / self.queue_wait_count) if self.queue_wait_count else 0.0
            return {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "timeout_count": self.timeout_count,
                "retry_count": self.retry_count,
                "client_reset_count": self.client_reset_count,
                "slow_request_count": self.slow_request_count,
                "slow_threshold_s": SLOW_REQUEST_THRESHOLD,
                "avg_latency_ms": round(avg * 1000, 2),
                "avg_queue_wait_ms": round(avg_queue * 1000, 2),
                "status_counts": dict(self.status_counts),
                "model_counts": dict(self.model_counts),
            }


metrics = Metrics()

# ==================== Concurrency Control ====================
kimi_semaphore = threading.Semaphore(MAX_CONCURRENT)
_active_requests_lock = threading.Lock()
_active_requests = 0
_shutdown_event = threading.Event()

# ==================== Upstream Health Probe ====================
class UpstreamHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self._healthy = True
        self._last_check = 0
        self._check_interval = 30

    def _do_probe(self):
        try:
            token = token_mgr.get_token()
            if not token:
                return False
            parsed = urllib.parse.urlparse(UPSTREAM_BASE)
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=5)
            try:
                conn.request(
                    "GET",
                    f"{parsed.path}/v1/models",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "KimiCLI/1.5",
                    },
                )
                resp = conn.getresponse()
                # 200 or 401 with valid auth means upstream is reachable
                healthy = resp.status in (200, 401)
            finally:
                conn.close()
            return healthy
        except Exception as e:
            logger.debug(f"Upstream probe failed: {e}")
            return False

    def check(self):
        with self._lock:
            if time.time() - self._last_check < self._check_interval:
                return self._healthy
        healthy = self._do_probe()
        with self._lock:
            if healthy != self._healthy:
                if healthy:
                    logger.info("Upstream health probe: healthy")
                else:
                    logger.warning("Upstream health probe: UNHEALTHY")
            self._healthy = healthy
            self._last_check = time.time()
        return healthy

    def is_healthy(self):
        with self._lock:
            # Stale check if too old (e.g. probe thread died)
            if time.time() - self._last_check > self._check_interval * 3:
                return False
            return self._healthy


upstream_health = UpstreamHealth()


def _upstream_probe_worker():
    check_interval = upstream_health._check_interval
    while not _shutdown_event.is_set():
        upstream_health.check()
        # Sleep in small chunks so we can exit quickly on shutdown
        for _ in range(check_interval):
            if _shutdown_event.is_set():
                break
            time.sleep(1)


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
                with self._lock:
                    return self._data.get("expires_at", 0) > time.time() + 10
            self._refreshing = True
        try:
            with self._lock:
                refresh_token = self._data.get("refresh_token", "")
                if not refresh_token:
                    logger.warning("No refresh_token available")
                    return False
                parsed_auth = urllib.parse.urlparse(AUTH_ENDPOINT)
                conn = http.client.HTTPSConnection(parsed_auth.netloc, timeout=30)
                try:
                    body = urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": refresh_token,
                    })
                    conn.request(
                        "POST",
                        parsed_auth.path,
                        body=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    resp = conn.getresponse()
                    resp_body = resp.read()
                    if resp.status != 200:
                        try:
                            err_summary = json.loads(resp_body)
                        except Exception:
                            err_summary = resp_body.decode("utf-8", errors="replace")[:200]
                        logger.error(f"Token refresh HTTP {resp.status}: {err_summary}")
                        return False
                    new_tokens = json.loads(resp_body)
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

# ==================== Background Threads ====================
def refresh_worker():
    while not _shutdown_event.is_set():
        for _ in range(REFRESH_INTERVAL):
            if _shutdown_event.is_set():
                return
            time.sleep(1)
        if token_mgr.should_refresh():
            logger.info("Token expiring, refreshing...")
            token_mgr.refresh()


# ==================== Error Classification ====================
def classify_error(e):
    """Classify an exception into a user-friendly error type."""
    if isinstance(e, socket.timeout):
        return "upstream_timeout", "Upstream read timed out (model may be thinking too long)"
    if isinstance(e, ConnectionResetError):
        return "connection_reset", "Upstream closed connection unexpectedly"
    if isinstance(e, BrokenPipeError):
        return "broken_pipe", "Connection broken while sending request"
    if isinstance(e, ssl.SSLError):
        return "ssl_error", f"TLS/SSL error: {e}"
    if isinstance(e, OSError) and e.errno in (61, 111, 51, 8):
        return "connection_refused", "Cannot connect to upstream (network or DNS issue)"
    err_name = type(e).__name__
    return err_name.lower(), str(e)


# ==================== Request Body Helpers ====================
_THINKING_BUDGET_MAP = {
    "low": 4000,
    "medium": 8000,
    "high": 16000,
}


def _maybe_inject_thinking(body_dict: dict) -> dict:
    """If client sends reasoning_effort without thinking, inject thinking param."""
    if not isinstance(body_dict, dict):
        return body_dict
    if "thinking" in body_dict:
        return body_dict
    effort = body_dict.get("reasoning_effort")
    if isinstance(effort, str):
        effort = effort.strip().lower()
        budget = _THINKING_BUDGET_MAP.get(effort, 8000)
        body_dict["thinking"] = {"type": "enabled", "budget_tokens": budget}
        logger.info(f"Injected thinking=budget_tokens:{budget} from reasoning_effort={effort}")
    return body_dict


def _safe_body_preview(body: bytes, max_len: int = 500) -> str:
    """Return a safe preview of body for debug logging."""
    return body[:max_len].decode("utf-8", errors="replace")


# ==================== Core Request Function ====================
def _do_kimi_request(method, target_url, body, headers, retries=0):
    parsed = urllib.parse.urlparse(target_url)
    host, path = parsed.netloc, parsed.path + ("?" + parsed.query if parsed.query else "")
    conn = http.client.HTTPSConnection(host, timeout=UPSTREAM_TIMEOUT)
    try:
        req_headers = dict(headers)
        req_headers["Connection"] = "close"
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        status = resp.status
        if status in (502, 429, 503) and retries < MAX_RETRIES:
            wait = BACKOFF_BASE * (2 ** retries)
            logger.warning(f"HTTP {status}, retry in {wait:.1f}s (attempt {retries+1})...")
            metrics.record_retry()
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

    # Override to suppress default stderr logging
    def log_message(self, format, *args):
        if self.path not in ("/healthz", "/metrics"):
            logger.info(f"{self.command} {self.path} -> {args[1]}")

    # Catch client disconnects early to avoid stderr spam
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
            client = self.client_address[0] if self.client_address else "unknown"
            logger.debug(f"Client {client} disconnected early: {type(e).__name__}")
            metrics.record_client_reset()
        except Exception as e:
            client = self.client_address[0] if self.client_address else "unknown"
            logger.error(f"Unhandled exception for client {client}: {e}", exc_info=True)

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "unknown"

    def _send_error(self, code, message, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def _forward(self, method):
        global _active_requests
        start_time = time.time()
        client_ip = self._client_ip()
        request_id = self.headers.get("x-request-id", "")
        if not request_id:
            request_id = f"kp-{uuid.uuid4().hex[:12]}"

        token = token_mgr.get_token()
        if not token:
            latency = time.time() - start_time
            metrics.record_request(latency)
            logger.warning(f"[{request_id}] {client_ip} -> 401 (no token)")
            self._send_error(401, "No Kimi Code token available")
            return

        # --- Body size guard ---
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            latency = time.time() - start_time
            metrics.record_request(latency, 413)
            logger.warning(f"[{request_id}] {client_ip} -> 413 (body {content_length} > {MAX_BODY_SIZE})")
            self._send_error(413, f"Request body too large: {content_length} bytes")
            return

        body = self.rfile.read(content_length) if content_length > 0 else b""
        body_len = len(body)

        if DEBUG_BODY and body:
            logger.debug(f"[{request_id}] Request body preview: {_safe_body_preview(body)}")

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

        # --- Body enrichment ---
        DEFAULT_MAX_TOKENS = 32768
        model_name = ""
        try:
            req_json = json.loads(body) if body else {}
            if isinstance(req_json, dict):
                model_name = req_json.get("model", "")
                if "max_tokens" not in req_json:
                    req_json["max_tokens"] = DEFAULT_MAX_TOKENS
                    logger.info(f"[{request_id}] Injected max_tokens={DEFAULT_MAX_TOKENS}")
                else:
                    logger.debug(f"[{request_id}] Preserving max_tokens={req_json['max_tokens']}")
                req_json = _maybe_inject_thinking(req_json)
                body = json.dumps(req_json).encode("utf-8")
        except Exception as e:
            logger.debug(f"[{request_id}] Body enrichment skipped: {e}")
            if DEBUG_BODY and body:
                logger.debug(f"[{request_id}] Raw body preview: {_safe_body_preview(body)}")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "KimiCLI/1.5",
            "Accept": "application/json",
            "X-Request-Id": request_id,
        }
        headers.update(token_mgr.get_headers())
        for key in ("openai-beta", "anthropic-version"):
            if key in self.headers:
                headers[key] = self.headers[key]

        acquired = False
        queue_start = time.time()
        try:
            acquired = kimi_semaphore.acquire(timeout=QUEUE_TIMEOUT)
            queue_wait = time.time() - queue_start
            if not acquired:
                latency = time.time() - start_time
                metrics.record_request(latency, 503, queue_wait)
                logger.warning(
                    f"[{request_id}] {client_ip} -> 503 (queue_full wait={queue_wait:.2f}s active={_active_requests}/{MAX_CONCURRENT})"
                )
                self._send_error(
                    503,
                    "Kimi API concurrency limit exceeded, try again later",
                    extra_headers={"Retry-After": str(min(120, UPSTREAM_TIMEOUT))},
                )
                return

            with _active_requests_lock:
                _active_requests += 1

            logger.info(
                f"[{request_id}] {client_ip} -> {method} {path} "
                f"body={body_len}b queue_wait={queue_wait:.2f}s active={_active_requests}/{MAX_CONCURRENT}"
            )
            resp, error = _do_kimi_request(method, target_url, body, headers)

            if resp and resp.status == 401:
                logger.warning(f"[{request_id}] {client_ip} -> 401, refreshing token...")
                if token_mgr.refresh():
                    new_token = token_mgr.get_token()
                    new_headers = {**headers, "Authorization": f"Bearer {new_token}"}
                    resp, error = _do_kimi_request(method, target_url, body, new_headers)
                    if resp and resp.status != 401:
                        logger.info(f"[{request_id}] Retry after refresh OK")
                    else:
                        logger.error(f"[{request_id}] Retry after refresh failed")
                else:
                    logger.error(f"[{request_id}] Token refresh failed")

            if resp:
                latency = time.time() - start_time
                status = resp.status
                metrics.record_request(latency, status, queue_wait, model_name)
                # Read response body for size logging
                try:
                    resp_body = resp.read()
                    resp_len = len(resp_body)
                except Exception as e:
                    logger.error(f"[{request_id}] Failed to read response body: {e}")
                    resp_body = b""
                    resp_len = 0
                finally:
                    resp.close()
                    if hasattr(resp, "_conn"):
                        resp._conn.close()
                if DEBUG_BODY and resp_body:
                    logger.debug(f"[{request_id}] Response body preview: {_safe_body_preview(resp_body)}")
                log_level = logger.warning if latency > SLOW_REQUEST_THRESHOLD else logger.info
                log_level(
                    f"[{request_id}] {client_ip} -> {status} "
                    f"latency={latency:.2f}s queue={queue_wait:.2f}s "
                    f"req={body_len}b resp={resp_len}b model={model_name or '-'}"
                )
                self._send_response(status, resp.getheaders(), resp_body)
            else:
                latency = time.time() - start_time
                err_type, err_msg = classify_error(error)
                if err_type == "upstream_timeout":
                    metrics.record_timeout()
                else:
                    metrics.record_request(latency, 502, queue_wait, model_name)
                logger.error(
                    f"[{request_id}] {client_ip} -> 502 ({err_type}) "
                    f"latency={latency:.2f}s queue={queue_wait:.2f}s: {err_msg}"
                )
                self._send_error(502, f"{err_type}: {err_msg}")
        finally:
            if acquired:
                with _active_requests_lock:
                    _active_requests -= 1
                kimi_semaphore.release()

    def _send_response(self, status, resp_headers, data):
        self.send_response(status)
        for header, value in resp_headers:
            hl = header.lower()
            if hl in ("connection", "transfer-encoding"):
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response write")
        finally:
            pass  # data already fully read; no conn to close here

    def do_GET(self):
        if self.path == "/healthz":
            upstream_ok = upstream_health.is_healthy()
            self.send_response(200 if upstream_ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            health = {
                "status": "ok" if upstream_ok else "degraded",
                "upstream_healthy": upstream_ok,
                "version": "2.7",
                "token_expires_at": token_mgr._data.get("expires_at", 0),
                "token_remaining": max(0, token_mgr._data.get("expires_at", 0) - time.time()),
                "concurrent_limit": MAX_CONCURRENT,
                "concurrent_active": _active_requests,
                "features": [
                    "connection_close",
                    "serial_upstream",
                    "backoff_retry",
                    "auto_refresh_401",
                    "full_xmsh_headers",
                    "thinking_injection",
                    "error_classification",
                    "metrics",
                    "structured_access_log",
                    "client_reset_guard",
                    "body_size_guard",
                    "slow_request_warning",
                    "model_stats",
                    "disk_space_guard",
                    "graceful_shutdown",
                    "upstream_health_probe",
                    "debug_body",
                ],
            }
            self.wfile.write(json.dumps(health).encode())
            self.wfile.flush()
            return

        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(metrics.snapshot()).encode())
            self.wfile.flush()
            return

        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, starting graceful shutdown...")
    _shutdown_event.set()


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    logger.info("Kimi Code Proxy v2.7 started")
    logger.info(f"  Listen: http://{PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"  Upstream: {UPSTREAM_BASE}")
    logger.info(f"  Concurrent: {MAX_CONCURRENT}, Upstream timeout: {UPSTREAM_TIMEOUT}s, Queue timeout: {QUEUE_TIMEOUT}s")
    logger.info(f"  Max body size: {MAX_BODY_SIZE} bytes")
    logger.info(f"  Slow request threshold: {SLOW_REQUEST_THRESHOLD}s")
    logger.info(f"  Log dir max: {LOG_DIR_MAX_BYTES} bytes")
    logger.info(f"  Graceful shutdown wait: {GRACEFUL_SHUTDOWN_WAIT}s")
    logger.info(f"  Debug body: {DEBUG_BODY}")

    # Start background threads
    threading.Thread(target=refresh_worker, daemon=True).start()
    threading.Thread(target=_upstream_probe_worker, daemon=True).start()

    # Run server in a thread so we can wait for shutdown signal
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()

    # Wait for shutdown signal
    _shutdown_event.wait()

    # Stop accepting new connections
    logger.info("Shutting down server...")
    server.shutdown()

    # Wait for active requests to complete
    wait_start = time.time()
    while _active_requests > 0 and (time.time() - wait_start) < GRACEFUL_SHUTDOWN_WAIT:
        time.sleep(0.1)

    if _active_requests > 0:
        logger.warning(f"Force shutdown with {_active_requests} active requests remaining")
    else:
        logger.info("All active requests completed, shutdown cleanly")

    server_thread.join(timeout=5)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
