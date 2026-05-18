#!/usr/bin/env bash
set -euo pipefail
cd /workspace
rm -rf publish-docker
npm install --omit=dev
mkdir -p publish-docker
cp package.json index.mjs publish-docker/
if [ -f package-lock.json ]; then
  cp package-lock.json publish-docker/
fi
cp -r node_modules publish-docker/node_modules
