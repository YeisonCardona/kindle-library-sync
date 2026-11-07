import os
import sys
import sqlite3
import tempfile
import zipfile

from pathlib import Path

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog

from qt.core import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QRadioButton,
    QDialogButtonBox,
    QButtonGroup,
    QIcon,
    QPixmap,
    QGroupBox,
)

from .kindle_series import (
    _make_series_cde_key,
    _upsert_series_entry,
    get_db,
    suppress_audit_triggers,
)


from qt.core import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QDialogButtonBox,
)


class KindleSSHDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Kindle SSH Connection")

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Enter your Kindle SSH credentials:"))

        # Connection settings
        form_layout = QFormLayout()

        self.host = QLineEdit("192.168.x.x")

        self.port = QLineEdit("2222")

        self.username = QLineEdit("root")

        self.password = QLineEdit("root")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        form_layout.addRow("IP address:", self.host)
        form_layout.addRow("Port:", self.port)
        form_layout.addRow("Username:", self.username)
        form_layout.addRow("Password:", self.password)

        layout.addLayout(form_layout)

        # Dialog buttons
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


class SynchronizeSeries(InterfaceAction):

    name = "Synchronize Series"

    action_spec = (
        "Synchronize Series",
        None,
        "Synchronize Series",
        None,
    )

    def _prepare_runtime_libraries(self):
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # Si estamos en desarrollo por symlink o carpeta directa
        target_lib_dir = os.path.join(plugin_dir, "_series_manager")
        real_lib_path = os.path.join(target_lib_dir, "lib")

        # Si la carpeta lib local de desarrollo existe, la usamos directamente
        if not os.path.exists(real_lib_path):
            # Fallback por si acaso se empaqueta como zip en producción
            if ".zip" in plugin_dir:
                zip_path = plugin_dir.split(".zip")[0] + ".zip"
                plugins_dir = os.path.dirname(zip_path)
                target_lib_dir = os.path.join(plugins_dir, "_series_manager")
                real_lib_path = os.path.join(target_lib_dir, "lib")
                if not os.path.exists(real_lib_path):
                    os.makedirs(target_lib_dir, exist_ok=True)
                    with zipfile.ZipFile(zip_path, "r") as z:
                        for filename in z.namelist():
                            if filename.startswith("lib/"):
                                z.extract(filename, target_lib_dir)

        if real_lib_path not in sys.path:
            sys.path.insert(0, real_lib_path)

    def genesis(self):
        resources = self.load_resources(["images/libros.png"])

        pixmap = QPixmap()
        loaded = pixmap.loadFromData(resources["images/libros.png"])

        if loaded:
            icon = QIcon(pixmap)
            self.qaction.setIcon(icon)

        self.qaction.triggered.connect(self.run)

    def cc_db_con(self):
        conn = get_db(self.cc_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def run(self):
        self._prepare_runtime_libraries()
        from .ssh_client import KindleSSHClient

        dialog = KindleSSHDialog(self.gui)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            credentials = dialog.get_credentials()

            client = KindleSSHClient()
            client.connect(**credentials)
        else:
            return

        if not client.is_connected():
            info_dialog(
                self.gui,
                "Connection error",
                "Unable to connect to the Kindle. Please check the IP address, SSH port, and connection settings.",
                show=True,
            )
            return

        db = self.gui.current_db.new_api

        categories = db.get_categories()
        series = categories.get("series", [])

        client.execute("cp -f /var/local/cc.db /var/local/cc.db.bk")
        self.cc_db_path = Path(tempfile.gettempdir()) / "cc.db"
        client.download("/var/local/cc.db", self.cc_db_path)

        conn = self.cc_db_con()

        stats = {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "unchanged": 0,
            "unmatched": 0,
        }

        try:
            conn.execute("BEGIN")

            with suppress_audit_triggers(conn):

                for serie in series:

                    # if serie.name != "Kusuriya no Hitorigoto":
                    #     continue

                    books = sorted(
                        (db.get_metadata(book_id) for book_id in serie.id_set),
                        key=lambda book: book.get("series_index", 0),
                    )

                    series_name = serie.name
                    series_cde_key = _make_series_cde_key(series_name)

                    series_id = f"urn:collection:1:asin-{series_cde_key}"

                    print(f"\nSeries: {series_name}")

                    # --------------------------------------------------
                    # 1. Construir el estado deseado desde Calibre.
                    # --------------------------------------------------

                    desired = {}
                    missing_books = []

                    for book in books:

                        title = book.title

                        rows = conn.execute(
                            """
                            SELECT
                                p_cdeKey,
                                p_titles_0_nominal
                            FROM Entries
                            WHERE p_type = 'Entry:Item'
                              AND p_titles_0_nominal = ?
                            """,
                            (title,),
                        ).fetchall()

                        if not rows:
                            missing_books.append(title)
                            stats["unmatched"] += 1
                            print(f"  [MISSING] {title}")
                            continue

                        if len(rows) > 1:
                            raise RuntimeError(
                                f"Ambiguous title match: {title}. "
                                f"Matches found: {len(rows)}"
                            )

                        cde_key = rows[0]["p_cdeKey"]

                        position = float(book.get("series_index", 1))

                        position_label = (
                            str(int(position))
                            if position.is_integer()
                            else str(position)
                        )

                        if cde_key in desired:
                            raise RuntimeError(
                                f"Duplicate Kindle book in series: "
                                f"{title} ({cde_key})"
                            )

                        desired[cde_key] = {
                            "position": position - 1,
                            "position_label": position_label,
                            "title": title,
                        }

                    # --------------------------------------------------
                    # 2. Consultar el estado actual en Kindle.
                    # --------------------------------------------------

                    rows = conn.execute(
                        """
                        SELECT
                            d_itemCdeKey,
                            d_itemPosition,
                            d_itemPositionLabel
                        FROM Series
                        WHERE d_seriesId = ?
                          AND d_itemType = 'Entry:Item'
                        """,
                        (series_id,),
                    ).fetchall()

                    current = {
                        row["d_itemCdeKey"]: {
                            "position": float(row["d_itemPosition"]),
                            "position_label": row["d_itemPositionLabel"],
                        }
                        for row in rows
                    }

                    desired_keys = set(desired)
                    current_keys = set(current)

                    # --------------------------------------------------
                    # 3. Calcular diferencias.
                    # --------------------------------------------------

                    to_add = desired_keys - current_keys

                    to_check = desired_keys & current_keys

                    to_update = {
                        key
                        for key in to_check
                        if (
                            desired[key]["position"] != current[key]["position"]
                            or desired[key]["position_label"]
                            != current[key]["position_label"]
                        )
                    }

                    # Solo eliminar si todos los libros de Calibre
                    # pudieron localizarse inequívocamente en Kindle.
                    to_remove = (
                        current_keys - desired_keys if not missing_books else set()
                    )

                    # --------------------------------------------------
                    # 4. Insertar relaciones nuevas.
                    # --------------------------------------------------

                    for key in sorted(to_add):

                        item = desired[key]

                        conn.execute(
                            """
                            INSERT OR REPLACE INTO Series (
                                d_seriesId,
                                d_itemCdeKey,
                                d_itemPosition,
                                d_itemPositionLabel,
                                d_itemType,
                                d_seriesOrderType
                            )
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                series_id,
                                key,
                                item["position"],
                                item["position_label"],
                                "Entry:Item",
                                "ordered",
                            ),
                        )

                        stats["added"] += 1

                        print(f"  [ADD] {item['title']} " f"({item['position_label']})")

                    # --------------------------------------------------
                    # 5. Actualizar posiciones modificadas.
                    # --------------------------------------------------

                    for key in sorted(to_update):

                        item = desired[key]

                        conn.execute(
                            """
                            UPDATE Series
                            SET
                                d_itemPosition = ?,
                                d_itemPositionLabel = ?,
                                d_seriesOrderType = 'ordered'
                            WHERE d_seriesId = ?
                              AND d_itemCdeKey = ?
                              AND d_itemType = 'Entry:Item'
                            """,
                            (
                                item["position"],
                                item["position_label"],
                                series_id,
                                key,
                            ),
                        )

                        stats["updated"] += 1

                        print(
                            f"  [UPDATE] {item['title']} " f"({item['position_label']})"
                        )

                    # --------------------------------------------------
                    # 6. Eliminar relaciones que ya no corresponden.
                    # --------------------------------------------------

                    for key in sorted(to_remove):

                        conn.execute(
                            """
                            DELETE FROM Series
                            WHERE d_seriesId = ?
                              AND d_itemCdeKey = ?
                              AND d_itemType = 'Entry:Item'
                            """,
                            (series_id, key),
                        )

                        stats["removed"] += 1

                        print(f"  [REMOVE] {key}")

                    # --------------------------------------------------
                    # 7. Actualizar p_seriesState de los libros afectados.
                    # --------------------------------------------------

                    affected_keys = to_add | to_remove

                    for key in affected_keys:

                        membership = conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM Series
                            WHERE d_itemCdeKey = ?
                              AND d_itemType = 'Entry:Item'
                            """,
                            (key,),
                        ).fetchone()[0]

                        conn.execute(
                            """
                            UPDATE Entries
                            SET p_seriesState = ?
                            WHERE p_cdeKey = ?
                              AND p_type = 'Entry:Item'
                            """,
                            (0 if membership else 1, key),
                        )

                    # --------------------------------------------------
                    # 8. Crear o actualizar el contenedor de la serie.
                    # --------------------------------------------------

                    total_in_series = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM Series
                        WHERE d_seriesId = ?
                        """,
                        (series_id,),
                    ).fetchone()[0]

                    if total_in_series:

                        _upsert_series_entry(
                            conn,
                            series_cde_key,
                            series_name,
                            total_in_series,
                        )

                    stats["unchanged"] += len(to_check) - len(to_update)

                    print(
                        f"  Added: {len(to_add)}, "
                        f"Updated: {len(to_update)}, "
                        f"Removed: {len(to_remove)}, "
                        f"Unchanged: {len(to_check) - len(to_update)}"
                    )

                    if missing_books:
                        print(
                            "  Deletions skipped because "
                            f"{len(missing_books)} books were not matched."
                        )

            conn.commit()

        except Exception:
            conn.rollback()
            info_dialog(
                self.gui,
                "Synchronization failed",
                str(e),
                show=True,
            )
            raise

        finally:
            # result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result = conn.execute("SELECT COUNT(*) FROM Entries").fetchone()[0]

            if result == 0:
                raise RuntimeError(f"Database integrity check failed: {result}")

            conn.close()

        if any(stats[key] for key in ("added", "updated", "removed")):

            # Upload modified database
            remote_tmp = "/var/local/cc.db.tmp"
            client.upload(self.cc_db_path, remote_tmp)

            # Replace original database
            client.execute("stop com.lab126.ccat")
            try:
                client.execute("mv -f /var/local/cc.db.tmp /var/local/cc.db")
            finally:
                client.execute("start com.lab126.ccat")

        info_dialog(
            self.gui,
            "Synchronization completed",
            f"Added:     {stats['added']}\n"
            f"Updated:   {stats['updated']}\n"
            f"Removed:   {stats['removed']}\n"
            f"Unchanged: {stats['unchanged']}\n"
            f"Unmatched: {stats['unmatched']}\n",
            show=True,
        )
