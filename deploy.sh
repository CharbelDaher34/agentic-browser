#!/usr/bin/env bash
# Deploy to FastAPI Cloud using the PROJECT-LOCAL auth token in ./.fastapicloud/
# (kept out of ~/.config and gitignored). Run `./deploy.sh` — the first run is
# interactive (pick team / create the app); later runs reuse the saved app id.
#
#   ./deploy.sh            # deploy (first run: choose team + app)
#   ./deploy.sh --no-wait  # don't block on the build
set -euo pipefail
cd "$(dirname "$0")"
export FASTAPI_CLOUD_CLI_CONFIG_DIR="$PWD/.fastapicloud"
exec fastapi deploy "$@"
