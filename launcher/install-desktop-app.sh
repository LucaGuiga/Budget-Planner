#!/bin/zsh
# Builds "Budget Planner.app" on the Desktop with the dollar-sign icon.
# Run it again if you move the budget-planner folder.
set -e
HERE="${0:A:h}"
PROJECT="${HERE:h}"
APP="$HOME/Desktop/Budget Planner.app"
# Store the folder relative to the home folder when possible, so the app still
# works on another Mac (for example through iCloud Desktop sync) with a different user name.
if [[ "$PROJECT" == "$HOME"/* ]]; then
  PROJECT_PATH="\$HOME/${PROJECT#$HOME/}"
else
  PROJECT_PATH="$PROJECT"
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/BudgetPlanner.icns" "$APP/Contents/Resources/BudgetPlanner.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Budget Planner</string>
  <key>CFBundleDisplayName</key><string>Budget Planner</string>
  <key>CFBundleIdentifier</key><string>local.budget-planner.launcher</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>BudgetPlanner</string>
  <key>CFBundleIconFile</key><string>BudgetPlanner</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/BudgetPlanner" <<LAUNCH
#!/bin/zsh
# If the planner is already running, just reopen its page. Otherwise start it
# in a Terminal window (close that window to stop it).
PROJECT="$PROJECT_PATH"
INFO="\$PROJECT/data/server.json"
if [[ -f "\$INFO" ]]; then
  pid=\$(/usr/bin/plutil -extract pid raw -o - "\$INFO" 2>/dev/null)
  url=\$(/usr/bin/plutil -extract url raw -o - "\$INFO" 2>/dev/null)
  if [[ -n "\$pid" && -n "\$url" ]] && /bin/ps -p "\$pid" -o command= 2>/dev/null | /usr/bin/grep -q "budget.py"; then
    /usr/bin/open "\$url"
    exit 0
  fi
fi
if [[ ! -f "\$PROJECT/budget.py" ]]; then
  /usr/bin/osascript -e 'display alert "Budget Planner" message "Can'"'"'t find the budget-planner folder. If you moved it, run launcher/install-desktop-app.sh again."'
  exit 1
fi
/usr/bin/open -a Terminal "\$PROJECT/launcher/Start Budget Planner.command"
LAUNCH

chmod +x "$APP/Contents/MacOS/BudgetPlanner" "$HERE/Start Budget Planner.command"
/usr/bin/codesign --force --sign - "$APP" >/dev/null 2>&1 || true   # ad-hoc signature so macOS runs it cleanly
/usr/bin/touch "$APP"                                               # refresh the icon in Finder
echo "Made $APP"
