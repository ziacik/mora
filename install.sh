#!/bin/sh
set -eu

PREFIX="${MORA_HOME:-$HOME/.local/share/mora}"
BIN="$HOME/.local/bin"
VENV="$PREFIX/venv"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --no-deps --no-build-isolation --upgrade "$ROOT"
mkdir -p "$BIN"
cat > "$BIN/mora" <<WRAP
#!/bin/sh
exec "$VENV/bin/mora" "\$@"
WRAP
chmod +x "$BIN/mora"

printf '%s\n' "Installed Mora to $VENV"
printf '%s\n' "Command: $BIN/mora"
printf '%s\n' "If 'mora' is not found, add $BIN to PATH."
