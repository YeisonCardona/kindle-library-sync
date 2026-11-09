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

# Create the plugin ZIP archive
echo "Generating synchronize_tags.zip..."
zip -r synchronize_tags.zip lib/ images/ __init__.py ui.py \
    ../ssh_client.py ../assets_db.py ../ui_dialog.py \
    plugin-import-name-synchronize_tags.txt
mv synchronize_tags.zip ../

# Install the plugin in Calibre
/Applications/calibre.app/Contents/MacOS/calibre-customize \
    -a ../synchronize_tags.zip

# Remove temporary build files
rm -rf ./lib

echo "Plugin installed successfully."