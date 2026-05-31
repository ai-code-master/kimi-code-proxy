#!/bin/bash
# Install launchd service for macOS

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_SRC="$SCRIPT_DIR/kimi-code-proxy.plist"
PLIST_DST="$HOME/Library/LaunchAgents/io.github.kimi-code-proxy.plist"

echo "Installing launchd service..."
echo "  Project dir: $PROJECT_DIR"

# Substitute paths
sed -e "s|/ABSOLUTE/PATH/TO/kimi-code-proxy|$PROJECT_DIR|g" \
    -e "s|/ABSOLUTE/PATH/TO/kimi_code_proxy.py|$PROJECT_DIR/kimi_code_proxy.py|g" \
    -e "s|/Users/YOUR_USERNAME|$HOME|g" \
    -e "s|io.github.YOUR_USERNAME.kimi-code-proxy|io.github.kimi-code-proxy|g" \
    "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
launchctl start io.github.kimi-code-proxy

echo "Done! Check status with: launchctl list | grep kimi-code-proxy"
