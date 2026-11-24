#!/bin/bash
# Forces PATH to prefer the portable Node 26 install (~/.local/node) over
# whatever system Node the preview tool's spawn environment would otherwise
# pick up (system Node here is 18.19.1, too old for Angular 22 -- see
# client/README.md "Running" for why). No sudo/system-wide install needed.
export PATH="$HOME/.local/node/bin:$PATH"
exec npm "$@"
