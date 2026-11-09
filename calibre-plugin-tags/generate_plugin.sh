#!/usr/bin/env bash

set -euo pipefail

# Clean previous build
rm -rf ./lib
rm -f ./synchronize_tags.zip

rm -rf "$HOME/Library/Preferences/calibre/plugins/_tags_manager"

mkdir -p ./lib

python3.14 -m pip install paramiko --target ./lib/ \
    --force-reinstall --no-cache-dir

# 5. Generate plugin ZIP
echo "Generating generate_comic_kfx.zip..."
zip -r synchronize_tags.zip lib/ images/ __init__.py ui.py ../ssh_client.py plugin-import-name-synchronize_tags.txt
mv synchronize_tags.zip ../

# 6. Install plugin
/Applications/calibre.app/Contents/MacOS/calibre-customize -a ../synchronize_tags.zip

rm -rf ./lib

echo "Plugin installed successfully."
