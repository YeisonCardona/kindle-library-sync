#!/usr/bin/env bash

set -euo pipefail

# Clean up the previous build and installed plugin support files
rm -rf ./lib
rm -f ../synchronize_series.zip
rm -rf "$HOME/Library/Preferences/calibre/plugins/_series_manager"

# Install Paramiko into the plugin package
mkdir -p ./lib
python3.14 -m pip install paramiko --target ./lib/ \
    --force-reinstall --no-cache-dir

cp ../ssh_client.py ssh_client.py
cp ../asset_db.py asset_db.py
cp ../ui_dialogs.py ui_dialogs.py

# Create the plugin ZIP archive
echo "Generating synchronize_series.zip..."
zip -r synchronize_series.zip lib/ images/ __init__.py ui_series.py \
    ssh_client.py asset_db.py ui_dialogs.py \
    plugin-import-name-synchronize_series.txt
mv synchronize_series.zip ../

# Install the plugin in Calibre
/Applications/calibre.app/Contents/MacOS/calibre-customize \
    -a ../synchronize_series.zip

# Remove temporary build files
rm -rf ./lib
rm ssh_client.py asset_db.py ui_dialogs.py

echo "Plugin installed successfully."