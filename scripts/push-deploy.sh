#!/bin/bash
# push-deploy.sh - Push to origin and deploy to server2 via deploy.sh
# Usage: ./scripts/push-deploy.sh [git-push-args...]
# Only deploys from main branch.

set -e

BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ "$BRANCH" != "main" ]; then
    echo "⚠️  Not on main branch (currently on $BRANCH), skipping deployment"
    git push "$@"
    exit 0
fi

if ! git diff-index --quiet HEAD --; then
    echo "⚠️  Uncommitted changes detected, please commit first"
    exit 1
fi

echo "📤 Pushing to origin..."
git push "$@"

if [ $? -eq 0 ]; then
    echo ""
    echo "🚀 Deploying to server2 via deploy.sh..."
    ssh server2-auto 'cd /srv/containers/mcproxy && sudo bash deploy.sh' && \
        echo "" && \
        echo "✅ Deployment complete!" || \
        echo "❌ Deployment failed"
fi