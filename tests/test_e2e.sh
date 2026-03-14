#!/bin/bash
# End-to-end integration tests for the full stack
# Usage: ./test_e2e.sh
set -e

FRONTEND_URL="http://localhost:80"
BACKEND_URL="http://localhost:8000"
PASS=0
FAIL=0

check() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    if [ "$actual" -eq "$expected" ]; then
        echo "  PASS: $name (HTTP $actual)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name (expected HTTP $expected, got HTTP $actual)"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== End-to-End Integration Tests ==="
echo ""

# 1. Frontend health
echo "[1] Frontend serves index.html"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$FRONTEND_URL/")
check "GET / (frontend)" 200 "$CODE"

# 2. Backend health
echo "[2] Backend health check"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BACKEND_URL/health")
check "GET /health (backend)" 200 "$CODE"

# 3. ML health (via docker internal network)
echo "[3] ML service health check (via docker exec)"
ML_HEALTH=$(docker compose exec -T backend python3 -c "import httpx; r = httpx.get('http://ml:8001/health'); print(r.status_code)" 2>/dev/null)
if [ "$ML_HEALTH" = "200" ]; then
    echo "  PASS: ML health check (HTTP 200)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: ML health check (got: $ML_HEALTH)"
    FAIL=$((FAIL + 1))
fi

# 4. Backend Socket.IO endpoint
echo "[4] Backend Socket.IO endpoint"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BACKEND_URL/socket.io/?EIO=4&transport=polling")
check "GET /socket.io/ (polling)" 200 "$CODE"

# 5. Frontend proxies Socket.IO
echo "[5] Frontend proxies Socket.IO"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$FRONTEND_URL/socket.io/?EIO=4&transport=polling")
check "GET /socket.io/ via nginx proxy" 200 "$CODE"

# 6. Frontend proxies /health
echo "[6] Frontend proxies /health"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$FRONTEND_URL/health")
check "GET /health via nginx proxy" 200 "$CODE"

# 7. ML Generate via docker exec (real LLM call)
echo "[7] ML Generate (real LLM call via docker exec - may take time...)"
RESPONSE=$(docker compose exec -T backend python3 -c "
import httpx, json, sys
r = httpx.post('http://ml:8001/generate', json={'description': 'Simple process: receive order, process payment, ship item'}, timeout=120.0)
print(json.dumps({'status': r.status_code, 'body': r.json()}))
" 2>/dev/null)

if [ -n "$RESPONSE" ]; then
    STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    if [ "$STATUS" = "200" ]; then
        echo "  PASS: POST /generate (HTTP 200)"
        PASS=$((PASS + 1))

        HAS_XML=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin)['body']; print('yes' if 'bpmn_xml' in d and 'definitions' in d['bpmn_xml'] else 'no')" 2>/dev/null || echo "no")
        if [ "$HAS_XML" = "yes" ]; then
            echo "  PASS: Response contains valid BPMN XML"
            PASS=$((PASS + 1))
        else
            echo "  FAIL: Response missing valid BPMN XML"
            FAIL=$((FAIL + 1))
        fi

        HAS_NAME=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin)['body']; print('yes' if 'session_name' in d and len(d['session_name']) > 0 else 'no')" 2>/dev/null || echo "no")
        if [ "$HAS_NAME" = "yes" ]; then
            echo "  PASS: Response contains session_name"
            PASS=$((PASS + 1))
        else
            echo "  FAIL: Response missing session_name"
            FAIL=$((FAIL + 1))
        fi
    else
        echo "  FAIL: POST /generate (HTTP $STATUS)"
        FAIL=$((FAIL + 1))
    fi
else
    echo "  FAIL: No response from ML generate"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=========================================="
echo "=== Results: $PASS passed, $FAIL failed ==="
echo "=========================================="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
