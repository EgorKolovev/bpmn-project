#!/bin/bash
# Smoke tests for ML service
# Usage: ./test_smoke.sh [BASE_URL]
set -e

BASE_URL="${1:-http://localhost:8001}"
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

echo "=== ML Service Smoke Tests ==="
echo "Target: $BASE_URL"
echo ""

# 1. Health check
echo "[1] Health check"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
check "GET /health" 200 "$CODE"

# 2. Generate - missing body
echo "[2] Generate - missing body"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/generate" -H "Content-Type: application/json" -d '{}')
check "POST /generate (empty body)" 422 "$CODE"

# 3. Generate - empty description
echo "[3] Generate - empty description"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/generate" -H "Content-Type: application/json" -d '{"description":""}')
check "POST /generate (empty description)" 422 "$CODE"

# 4. Edit - missing fields
echo "[4] Edit - missing fields"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/edit" -H "Content-Type: application/json" -d '{}')
check "POST /edit (empty body)" 422 "$CODE"

# 5. Edit - partial fields
echo "[5] Edit - partial fields"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/edit" -H "Content-Type: application/json" -d '{"prompt":"test"}')
check "POST /edit (missing bpmn_xml)" 422 "$CODE"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
