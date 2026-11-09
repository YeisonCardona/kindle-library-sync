#!/usr/bin/env bash

set -euo pipefail

# Clean previous build
rm -rf ./lib
rm -f ../synchronize_series.zip

rm -rf "$HOME/Library/Preferences/calibre/plugins/_series_manager"

mkdir -p ./lib

python3.14 -m pip install paramiko --target ./lib/ \
    --force-reinstall --no-cache-dir

# 5. Generate plugin ZIP
echo "Generating generate_comic_kfx.zip..."
zip -r synchronize_series.zip lib/ images/ __init__.py ui.py ../ssh_client.py plugin-import-name-synchronize_series.txt
mv synchronize_series.zip ../

# 6. Install plugin
/Applications/calibre.app/Contents/MacOS/calibre-customize -a ../synchronize_series.zip

rm -rf ./lib

echo "Plugin installed successfully."
