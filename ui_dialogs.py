"""Qt dialogs and UI packaging helpers shared by the Calibre actions."""

import os
import sys
import zipfile

from qt.core import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QCheckBox,
    QGroupBox,
)


class KindleSSHDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kindle SSH Connection")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your Kindle SSH credentials:"))

        form = QFormLayout()
        self.host, self.port = QLineEdit("192.168.x.x"), QLineEdit("2222")
        self.username, self.password = QLineEdit("root"), QLineEdit("root")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("IP address:", self.host)
        form.addRow("Port:", self.port)
        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)
        layout.addLayout(form)

        # Synchronization options
        sync_group = QGroupBox("Synchronization options")
        sync_layout = QVBoxLayout(sync_group)

        self.sync_series = QCheckBox("Synchronize Series")
        self.sync_series.setChecked(True)

        self.sync_tags = QCheckBox("Synchronize Tags")
        self.sync_tags.setChecked(True)

        sync_layout.addWidget(self.sync_series)
        sync_layout.addWidget(self.sync_tags)
        layout.addWidget(sync_group)

        self.restart_after_sync = QCheckBox(
            "Restart Kindle Library after synchronization"
        )
        self.restart_after_sync.setChecked(False)
        layout.addWidget(self.restart_after_sync)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_credentials(self):
        return {
            "host": self.host.text().strip(),
            "port": int(self.port.text().strip()),
            "username": self.username.text().strip(),
            "password": self.password.text(),
        }

    def restart_requested(self):
        return self.restart_after_sync.isChecked()

    def sync_options(self):
        return {
            "series": self.sync_series.isChecked(),
            "tags": self.sync_tags.isChecked(),
        }


def calibre_book_identity(book):
    """Convert a Calibre metadata object to plain title/identifier data."""
    identifiers = getattr(book, "identifiers", None)
    if callable(identifiers):
        identifiers = identifiers()
    if not isinstance(identifiers, dict):
        try:
            identifiers = book.get("identifiers", {})
        except Exception:
            identifiers = {}
    return book.title, identifiers if isinstance(identifiers, dict) else {}


def prepare_runtime_libraries(module_file, folder_name):
    """Expose optional bundled SSH libraries when running as a Calibre ZIP plugin."""
    plugin_dir = os.path.dirname(os.path.abspath(module_file))
    lib_dir = os.path.join(plugin_dir, folder_name, "lib")
    if not os.path.isdir(lib_dir) and ".zip" in plugin_dir:
        zip_path = plugin_dir.split(".zip", 1)[0] + ".zip"
        target = os.path.join(os.path.dirname(zip_path), folder_name)
        lib_dir = os.path.join(target, "lib")
        if not os.path.isdir(lib_dir) and os.path.exists(zip_path):
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(zip_path) as archive:
                for filename in archive.namelist():
                    if filename.startswith("lib/"):
                        archive.extract(filename, target)
    if os.path.isdir(lib_dir) and lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
