#!/usr/bin/env bash
#
# Deploy negrisk bot to a remote server
#
# Usage:
#   ./scripts/deploy.sh <user@host> [scanner|dryrun|live]
#
# Prerequisites:
#   - SSH access to the remote server
#   - Docker + Docker Compose installed on remote
#   - .env file with secrets (POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, ALERT_WEBHOOK_URL)
#
# Examples:
#   ./scripts/deploy.sh root@1.2.3.4              # Deploy scanner-only
#   ./scripts/deploy.sh root@1.2.3.4 dryrun       # Deploy with dry-run execution
#   ./scripts/deploy.sh root@1.2.3.4 live          # Deploy with LIVE execution
#
# Recommended VPS providers (non-US regions):
#   - Hetzner (Germany/Finland): hetzner.com - CX22 ~4 EUR/mo
#   - DigitalOcean (Singapore/Amsterdam): digitalocean.com - $6/mo
#   - AWS EC2 (eu-west-1 Ireland): t3.micro ~$8/mo
#   - Vultr (Amsterdam/Tokyo): vultr.com - $5/mo

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Args
REMOTE_HOST="${1:?Usage: $0 <user@host> [scanner|dryrun|live]}"
MODE="${2:-scanner}"
REMOTE_DIR="/opt/negrisk"

echo -e "${GREEN}=== Negrisk Bot Deployment ===${NC}"
echo "Target: ${REMOTE_HOST}"
echo "Mode: ${MODE}"
echo "Remote dir: ${REMOTE_DIR}"
echo ""

# Validate mode
case "$MODE" in
    scanner)
        SERVICE="negrisk-scanner"
        ;;
    dryrun)
        SERVICE="negrisk-executor-dryrun"
        ;;
    live)
        SERVICE="negrisk-executor-live"
        echo -e "${RED}WARNING: LIVE mode will place real orders with real money!${NC}"
        read -p "Are you sure? (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            echo "Aborted."
            exit 1
        fi
        ;;
    *)
        echo -e "${RED}Invalid mode: $MODE. Use scanner|dryrun|live${NC}"
        exit 1
        ;;
esac

# Check for .env file
if [ ! -f ".env" ] && [ "$MODE" != "scanner" ]; then
    echo -e "${YELLOW}Warning: No .env file found. Execution modes require:${NC}"
    echo "  POLYMARKET_PRIVATE_KEY=0x..."
    echo "  POLYMARKET_FUNDER=0x..."
    echo "  ALERT_WEBHOOK_URL=https://..."
    echo ""
    echo "Create .env file and re-run, or press Enter to continue without it."
    read
fi

echo -e "${GREEN}[1/5] Creating remote directory...${NC}"
ssh "$REMOTE_HOST" "mkdir -p ${REMOTE_DIR}/logs/negrisk/recordings"

echo -e "${GREEN}[2/5] Syncing project files...${NC}"
# Use rsync to sync only necessary files (respects .dockerignore concept)
rsync -avz --progress \
    --exclude='.git/' \
    --exclude='venv/' \
    --exclude='.venv/' \
    --exclude='logs/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.claude/' \
    --exclude='*.mp4' \
    --exclude='*.png' \
    --exclude='worktrees/' \
    --exclude='tests/' \
    --exclude='test_*.py' \
    --exclude='*.sh' \
    --include='scripts/deploy.sh' \
    ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

# Sync .env if it exists (with restrictive permissions)
if [ -f ".env" ]; then
    echo -e "${GREEN}[2.5/5] Syncing .env file...${NC}"
    scp .env "${REMOTE_HOST}:${REMOTE_DIR}/.env"
    ssh "$REMOTE_HOST" "chmod 600 ${REMOTE_DIR}/.env"
fi

echo -e "${GREEN}[3/5] Building Docker image on remote...${NC}"
ssh "$REMOTE_HOST" "cd ${REMOTE_DIR} && docker compose build --no-cache"

echo -e "${GREEN}[4/5] Starting service: ${SERVICE}...${NC}"
# Stop any existing services first
ssh "$REMOTE_HOST" "cd ${REMOTE_DIR} && docker compose down 2>/dev/null || true"
ssh "$REMOTE_HOST" "cd ${REMOTE_DIR} && docker compose up -d ${SERVICE}"

echo -e "${GREEN}[5/5] Verifying deployment...${NC}"
sleep 3
ssh "$REMOTE_HOST" "cd ${REMOTE_DIR} && docker compose ps"
echo ""
ssh "$REMOTE_HOST" "cd ${REMOTE_DIR} && docker compose logs --tail=20 ${SERVICE}"

echo ""
echo -e "${GREEN}=== Deployment Complete ===${NC}"
echo ""
echo "Useful commands:"
echo "  # View live logs:"
echo "  ssh ${REMOTE_HOST} 'cd ${REMOTE_DIR} && docker compose logs -f ${SERVICE}'"
echo ""
echo "  # Check status:"
echo "  ssh ${REMOTE_HOST} 'cd ${REMOTE_DIR} && docker compose ps'"
echo ""
echo "  # Stop:"
echo "  ssh ${REMOTE_HOST} 'cd ${REMOTE_DIR} && docker compose down'"
echo ""
echo "  # Pull logs locally:"
echo "  rsync -avz ${REMOTE_HOST}:${REMOTE_DIR}/logs/ ./logs/remote/"
echo ""
echo "  # SSH in:"
echo "  ssh ${REMOTE_HOST}"
