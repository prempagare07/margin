#!/usr/bin/env bash
# Usage: ./scripts/deploy.sh [agents|web|all]
# Pulls latest code and rebuilds Docker images on the server.
set -euo pipefail

TARGET="${1:-all}"
SERVER="root@inappmargin.vip"
REPO="/opt/margin"

echo "=== Pulling latest code ==="
ssh "$SERVER" "cd $REPO && git pull origin main"

if [[ "$TARGET" == "agents" || "$TARGET" == "all" ]]; then
  echo "=== Rebuilding agents image ==="
  ssh "$SERVER" "cd $REPO && docker-compose -f deploy/docker-compose.yml build agents"
  echo "=== Restarting agents ==="
  ssh "$SERVER" "cd $REPO && docker-compose -f deploy/docker-compose.yml up -d agents"
fi

if [[ "$TARGET" == "web" || "$TARGET" == "all" ]]; then
  echo "=== Rebuilding web image ==="
  ssh "$SERVER" "cd $REPO && docker-compose -f deploy/docker-compose.yml build web"
  echo "=== Restarting web ==="
  ssh "$SERVER" "cd $REPO && docker-compose -f deploy/docker-compose.yml up -d web"
fi

echo "=== Done. Checking logs ==="
ssh "$SERVER" "docker logs deploy-agents-1 --tail=6 2>&1"
