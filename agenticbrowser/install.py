# SPDX-License-Identifier: Apache-2.0
"""`agenticbrowser-install` (or `python -m agenticbrowser.install`).

Fetches the Chromium binary Playwright needs for the local browser backend.
Not needed when you only use `backend="browserbase"` (cloud browsers)."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    print("Installing Playwright Chromium for the local browser backend…")
    try:
        rc = subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    except FileNotFoundError:
        print(
            "Playwright is not installed. Install the package first: "
            "`pip install agenticbrowser`.",
            file=sys.stderr,
        )
        return 1
    if rc == 0:
        print("Done — Chromium is ready. Use BrowserAgent(backend='local').")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
