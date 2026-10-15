#!/bin/sh
# Install Budget Planner on Linux. In a terminal: sh "Install on Linux.sh"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Budget Planner needs Python 3.9 or newer. Install it with your package"
  echo "manager (for example: sudo apt install python3), then run this again."
  exit 1
fi
exec python3 "$(dirname "$0")/app/install.py"
