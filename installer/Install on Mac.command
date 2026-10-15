#!/bin/zsh
# Double-click to install Budget Planner on this Mac.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Budget Planner needs Python 3.9 or newer."
  echo
  echo "If a box just asked to install Apple's command line developer tools,"
  echo "click Install, wait for it to finish, then double-click this installer again."
  echo "Otherwise, get Python from https://www.python.org/downloads/ and try again."
  echo
  read -k1 "?Press any key to close."
  exit 1
fi
python3 "${0:A:h}/app/install.py"
echo
read -k1 "?Press any key to close."
