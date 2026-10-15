#!/bin/zsh
# Starts the budget planner in this Terminal window. Close the window (or press
# Ctrl+C) to stop it. The Desktop app opens this file.
cd "${0:A:h}/.." || exit 1
python3 budget.py
