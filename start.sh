#!/bin/bash
# Quick start script for Kimi Code Proxy

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present (safely handles values with spaces)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Validate required env
if [ -z "$KCP_CLIENT_ID" ]; then
    echo "ERROR: KCP_CLIENT_ID is not set. Please configure .env file."
    exit 1
fi

echo "Starting Kimi Code Proxy..."
exec python3 kimi_code_proxy.py
