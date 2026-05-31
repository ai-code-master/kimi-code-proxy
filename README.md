# Kimi Code OAuth Proxy

A lightweight local HTTP proxy that bridges **OpenAI-compatible clients** (e.g. [Hermes](https://github.com/nousresearch/hermes)) to the **Kimi Code API**.

Kimi Code uses OAuth2 + Anthropic Messages API format, which most tools don't speak natively. This proxy handles:

- **OAuth token refresh** automatically (no manual token copy-paste)
- **Protocol translation** — exposes an OpenAI-compatible `/v1/chat/completions` endpoint on `localhost`
- **Concurrency control** — serializes upstream requests to avoid Kimi API timeouts
- **Transparent failover** — retries on 502/429/503 with exponential backoff

## Why?

Hermes (and many other AI coding assistants) expects an OpenAI-compatible API. Kimi Code speaks Anthropic Messages API and uses OAuth. This proxy sits in the middle so you can use Kimi Code with any OpenAI-compatible tool.

```
Hermes / OpenAI client
       │  OpenAI protocol
       ▼
┌──────────────────────┐
│  Kimi Code Proxy     │  ← this project
│  http://127.0.0.1:8765│
└──────────────────────┘
       │  Anthropic Messages API + OAuth
       ▼
   https://api.kimi.com/coding
```

## Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/YOUR_USERNAME/kimi-code-proxy.git
cd kimi-code-proxy
cp .env.example .env
# Edit .env and set KCP_CLIENT_ID
```

### 2. Get OAuth Credentials

You need a valid Kimi Code OAuth token. The proxy reads it from `~/.kimi/credentials/kimi-code.json` (same format as the official Kimi CLI).

If you already use [Kimi CLI](https://kimi.com), the credentials file usually exists.

### 3. Run

```bash
./start.sh
```

Or directly:

```bash
python3 kimi_code_proxy.py
```

The proxy listens on `http://127.0.0.1:8765` by default.

### 4. Configure Your Client

Point your client to the proxy:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8765
export OPENAI_API_KEY=kimi-code-oauth   # any non-empty string works
```

For **Hermes**, add to `~/.hermes/.env`:

```bash
KIMI_BASE_URL=http://127.0.0.1:8765
KIMI_API_KEY=kimi-code-oauth
```

## Configuration

All settings are via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `KCP_CLIENT_ID` | *(required)* | OAuth client ID |
| `KCP_CREDENTIALS_PATH` | `~/.kimi/credentials/kimi-code.json` | Path to Kimi OAuth credentials |
| `KCP_DEVICE_ID_PATH` | `~/.kimi/device_id` | Path to device ID file |
| `KCP_AUTH_ENDPOINT` | `https://auth.kimi.com/api/oauth/token` | OAuth token endpoint |
| `KCP_UPSTREAM_BASE` | `https://api.kimi.com/coding` | Kimi Code API base URL |
| `KCP_HOST` | `127.0.0.1` | Proxy listen host |
| `KCP_PORT` | `8765` | Proxy listen port |
| `KCP_MAX_CONCURRENT` | `1` | Max concurrent upstream requests |
| `KCP_LOG_DIR` | `~/.hermes/logs` | Log directory |
| `KCP_DEVICE_NAME` | `KimiProxy` | Override to hide real device name |

## Run as macOS Service (launchd)

Copy the provided plist template and update paths:

```bash
cp launchd/kimi-code-proxy.plist ~/Library/LaunchAgents/
# Edit the plist to set the correct WorkingDirectory and ProgramArguments
launchctl load ~/Library/LaunchAgents/kimi-code-proxy.plist
launchctl start kimi-code-proxy
```

## Health Check

```bash
curl http://127.0.0.1:8765/healthz
```

## License

MIT
