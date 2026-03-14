#!/bin/bash
# Manual curl tests for ML service (requires real LLM API key)
# Usage: ./test_manual_curls.sh [BASE_URL]
set -e

BASE_URL="${1:-http://localhost:8001}"

echo "=== ML Service Manual Curl Tests ==="
echo "Target: $BASE_URL"
echo ""

# 1. Health check
echo "[1] Health check"
curl -s "$BASE_URL/health" | python3 -m json.tool
echo ""

# 2. Generate a BPMN diagram
echo "[2] Generate BPMN diagram"
echo "Sending: hiring process description..."
RESPONSE=$(curl -s -X POST "$BASE_URL/generate" \
    -H "Content-Type: application/json" \
    -d '{"description": "Employee hiring process: HR receives application, reviews resume, conducts phone interview, schedules in-person interview, makes hiring decision, sends offer letter or rejection."}')
echo "Response:"
echo "$RESPONSE" | python3 -m json.tool
echo ""

# Extract bpmn_xml for edit test
BPMN_XML=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['bpmn_xml'])")

# 3. Edit the diagram
echo "[3] Edit BPMN diagram"
echo "Sending: add background check step..."
curl -s -X POST "$BASE_URL/edit" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json; print(json.dumps({'prompt': 'Add a background check step after the in-person interview', 'bpmn_xml': open('/dev/stdin').read()}))" <<< "$BPMN_XML")" | python3 -m json.tool
echo ""

echo "=== Manual tests complete ==="
