#!/bin/sh
set -e

if [ "${PLAYWRIGHT_INSTALL_ON_START:-1}" != "0" ]; then
  echo "Ensuring Python dependencies are installed..."
  python -m pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.txt
  if ! python -c "import playwright" >/dev/null 2>&1; then
    echo "Playwright Python package is still missing; installing it explicitly..."
    python -m pip install --no-cache-dir --root-user-action=ignore "playwright==1.60.0"
  fi
  python -c "import playwright" >/dev/null 2>&1
  echo "Ensuring Playwright Chromium is installed..."
  python -m playwright install --with-deps chromium
fi

exec "$@"
