# SPDX-License-Identifier: Apache-2.0
"""Top-level ASGI entrypoint shim — re-exports the gateway app as `main:app`.

The real app lives in `agenticbrowser/server/gateway.py`. `infra/run.sh` and the
Docker image run `agenticbrowser.server.gateway:app` directly; this shim just lets
`uvicorn main:app` work too.
"""

from agenticbrowser.server.gateway import app  # noqa: F401
