#!/usr/bin/env bash
# 对本地 MCP 测试服务做一次 tools/list + tools/call 冒烟
set -euo pipefail
BASE="${1:-http://127.0.0.1:8765}"

echo "== health =="
curl -sS "$BASE/health" | head -c 500
echo ""
echo ""

echo "== initialize =="
curl -sS -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
echo ""
echo ""

echo "== tools/list =="
curl -sS -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
echo ""
echo ""

echo "== tools/call echo =="
curl -sS -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello-mcp"}}}'
echo ""
echo ""

echo "== tools/call add =="
curl -sS -X POST "$BASE/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"add","arguments":{"a":1.5,"b":2.5}}}'
echo ""
