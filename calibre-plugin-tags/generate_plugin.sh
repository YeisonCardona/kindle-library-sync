#!/usr/bin/env bash

set -euo pipefail

# Clean up the previous build and installed plugin support files
rm -rf ./lib
rm -f ./synchronize_tags.zip
rm -rf "$HOME/Library/Preferences/calibre/plugins/_tags_manager"

# Install Paramiko into the plugin package
mkdir -p ./lib
python3.14 -m pip install paramiko --target ./lib/ \
    --force-reinstall --no-cache-dir

cp ../ssh_client.py ssh_client.py
cp ../asset_db.py asset_db.py
cp ../ui_dialogs.py ui_dialogs.py

# Create the plugin ZIP archive
echo "Generating synchronize_tags.zip..."
zip -r synchronize_tags.zip lib/ images/ __init__.py ui_tags.py \
    ssh_client.py asset_db.py ui_dialogs.py \
    plugin-import-name-synchronize_tags.txt
mv synchronize_tags.zip ../

# Install the plugin in Calibre
/Applications/calibre.app/Contents/MacOS/calibre-customize \
    -a ../synchronize_tags.zip

# Remove temporary build files
rm -rf ./lib
rm ssh_client.py asset_db.py ui_dialogs.py

echo "Plugin installed successfully."