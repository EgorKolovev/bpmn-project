#!/bin/bash
# Smoke tests for Backend service
# Usage: ./test_smoke.sh [BASE_URL]
set -e

BASE_URL="${1:-http://localhost:8000}"
PASS=0
FAIL=0

check() {
    local name="$1"
    local expected_code="$2"
    local actual_code="$3"
    if [ "$actual_code" -eq "$expected_code" ]; then
        echo "  PASS: $name (HTTP $actual_code)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (expected HTTP $expected_code, got HTTP $actual_code)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Backend Service Smoke Tests ==="
echo "Target: $BASE_URL"
echo ""

# 1. Health check
echo "[1] Health check"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
check "GET /health" 200 "$CODE"

# 2. Socket.IO polling endpoint exists
echo "[2] Socket.IO endpoint reachable"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/socket.io/?EIO=4&transport=polling")
check "GET /socket.io/ (polling)" 200 "$CODE"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
