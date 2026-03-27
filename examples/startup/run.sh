#!/bin/bash
set -e

echo "=== Starting Hive: 3-Agent Startup ==="
echo ""

# Initialize org-memory repo if not exists
if [ ! -d "./org-memory" ]; then
    echo "Initializing org-memory..."
    mkdir -p org-memory
    cd org-memory && git init && cd ..
fi

echo "Starting registry on port 8080..."
hive registry --port 8080 &
REGISTRY_PID=$!
sleep 2

echo "Starting CEO on port 8462..."
hive join --config ceo.yaml --registry http://127.0.0.1:8080 --org-memory ./org-memory --port 8462 &
CEO_PID=$!
sleep 2

echo "Starting VP Marketing on port 8463..."
hive join --config vp-marketing.yaml --registry http://127.0.0.1:8080 --org-memory ./org-memory --port 8463 &
VP_PID=$!
sleep 2

echo "Starting SEO Agent on port 8464..."
hive join --config seo-agent.yaml --registry http://127.0.0.1:8080 --org-memory ./org-memory --port 8464 &
SEO_PID=$!
sleep 2

echo ""
echo "=== All agents running ==="
echo "  Registry:      http://127.0.0.1:8080"
echo "  CEO:           http://127.0.0.1:8462"
echo "  VP Marketing:  http://127.0.0.1:8463"
echo "  SEO Agent:     http://127.0.0.1:8464"
echo ""
echo "Open dashboard:  hive dashboard --registry http://127.0.0.1:8080 --org-memory ./org-memory"
echo "Check status:    hive status --registry http://127.0.0.1:8080"
echo ""
echo "Send a task to the CEO:"
echo "  curl -X POST http://127.0.0.1:8462 -H 'Content-Type: application/json' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"method\":\"message/send\",\"id\":\"1\",\"params\":{\"message\":{\"role\":\"user\",\"parts\":[{\"kind\":\"text\",\"text\":\"Prepare Q1 board deck\"}]}}}'"
echo ""
echo "Press Ctrl+C to stop all agents."

cleanup() {
    echo ""
    echo "Stopping agents..."
    kill $SEO_PID $VP_PID $CEO_PID $REGISTRY_PID 2>/dev/null || true
    wait 2>/dev/null
    echo "All agents stopped."
}
trap cleanup EXIT INT TERM

wait
