import json
import os
import sqlite3
import tempfile
import unicodedata
import zipfile
from pathlib import Path
import sys

from calibre.gui2 import info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import (
    QCheckBox,
    QCollator,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QLocale,
    QPixmap,
    QIcon,
    QVBoxLayout,
)


def get_db(path=None):
    if not path.exists():
        print(f"ERROR: {path} not found.")
        print("Copy cc.db from your Kindle (/var/local/cc.db) to this directory.")
        sys.exit(1)
    conn = sqlite3.connect(str(path))
    conn.create_collation("icu", lambda a, b: (a > b) - (a < b))
    return conn


def _normalize(value):
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(value.split())


def _make_series_cde_key(series_name):
    """Generate a deterministic cdeKey for a series based on its name."""
    return "SL-" + series_name.upper().replace(" ", "-").replace("'", "")


class KindleSSHDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kindle SSH Connection")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your Kindle SSH credentials:"))

        form = QFormLayout()
        self.host = QLineEdit("192.168.x.x")
        self.port = QLineEdit("2222")
        self.username = QLineEdit("root")
        self.password = QLineEdit("root")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("IP address:", self.host)
        form.addRow("Port:", self.port)
        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)
        layout.addLayout(form)

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
    """Synchronize Calibre series into ksdk.asset.db; never opens cc.db."""

    name = "Synchronize Series"

    action_spec = (
        "Synchronize Series",
        None,
        "Synchronize Series",
        None,
    )

    def _prepare_runtime_libraries(self):
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        target_lib_dir = os.path.join(plugin_dir, "_series_manager")
        real_lib_path = os.path.join(target_lib_dir, "lib")

        if not os.path.exists(real_lib_path) and ".zip" in plugin_dir:
            zip_path = plugin_dir.split(".zip")[0] + ".zip"
            plugins_dir = os.path.dirname(zip_path)
            target_lib_dir = os.path.join(plugins_dir, "_series_manager")
            real_lib_path = os.path.join(target_lib_dir, "lib")
            if not os.path.exists(real_lib_path):
                os.makedirs(target_lib_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as archive:
                    for filename in archive.namelist():
                        if filename.startswith("lib/"):
                            archive.extract(filename, target_lib_dir)

        if (
            os.path.exists(real_lib_path)
            and real_lib_path not in __import__("sys").path
        ):
            __import__("sys").path.insert(0, real_lib_path)

    def genesis(self):
        resources = self.load_resources(["images/libros.png"])

        pixmap = QPixmap()
        loaded = pixmap.loadFromData(resources["images/libros.png"])

        if loaded:
            icon = QIcon(pixmap)
            self.qaction.setIcon(icon)

        self.qaction.triggered.connect(self.run)

    @staticmethod
    def _asset_connection(path):
        conn = get_db(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                "SELECT TITLE_COLLATION FROM Nodes "
                "ORDER BY TITLE_COLLATION COLLATE icu LIMIT 0"
            ).fetchall()
        except sqlite3.OperationalError as err:
            if "collation" not in str(err).lower():
                conn.close()
                raise
            collator = QCollator(QLocale())
            conn.create_collation(
                "icu",
                lambda left, right: collator.compare(left or "", right or ""),
            )
        return conn

    @staticmethod
    def _json_or(value, fallback):
        try:
            return json.loads(value) if value else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _book_identifiers(book):
        identifiers = getattr(book, "identifiers", None)
        if callable(identifiers):
            identifiers = identifiers()
        if not isinstance(identifiers, dict):
            try:
                identifiers = book.get("identifiers", {})
            except Exception:
                identifiers = {}
        return {_normalize(value) for value in (identifiers or {}).values() if value}

    def _load_node_index(self, conn):
        rows = conn.execute("""
            SELECT INTERNAL_ID, ASSET_ID, ASIN, TITLE, TITLE_COLLATION,
                   ADDITIONAL_DATA, TYPE
            FROM Nodes
            WHERE TYPE = 'BOOK' AND ASSET_ID LIKE '%!!EBOK'
            """).fetchall()

        by_identifier = {}
        by_title = {}
        node_by_asset = {}

        for row in rows:
            asset_id = row["ASSET_ID"]
            asin = row["ASIN"] or asset_id.rsplit("!!", 1)[0]
            node_by_asset[asset_id] = row

            for identifier in (asset_id, asset_id.rsplit("!!", 1)[0], asin):
                key = _normalize(identifier)
                if key:
                    by_identifier.setdefault(key, set()).add(asset_id)

            titles = {row["TITLE"], row["TITLE_COLLATION"]}
            additional = self._json_or(row["ADDITIONAL_DATA"], {})
            if isinstance(additional, dict):
                titles.update(
                    additional.get(key)
                    for key in ("title", "title_collation", "titleCollation")
                )
                for title_item in additional.get("titles", []) or []:
                    if isinstance(title_item, dict):
                        titles.update(
                            title_item.get(key)
                            for key in ("display", "collation", "title")
                        )

            for title in titles:
                key = _normalize(title)
                if key:
                    by_title.setdefault(key, set()).add(asset_id)

        return node_by_asset, by_identifier, by_title

    def _match_book(self, book, node_by_asset, by_identifier, by_title):
        identifier_matches = set()
        for identifier in self._book_identifiers(book):
            identifier_matches.update(by_identifier.get(identifier, set()))
        if len(identifier_matches) == 1:
            return next(iter(identifier_matches))
        if len(identifier_matches) > 1:
            raise RuntimeError(
                f"Ambiguous identifier match for Calibre book: {book.title}"
            )

        title_matches = by_title.get(_normalize(book.title), set())
        if len(title_matches) == 1:
            return next(iter(title_matches))
        if len(title_matches) > 1:
            raise RuntimeError(f"Ambiguous title match: {book.title}")
        return None

    def _ensure_tag(self, conn, tag_id, series_name, count):
        row = conn.execute(
            "SELECT TAG_INTERNAL_ID, ADDITIONAL_DATA FROM Tags "
            "WHERE TAG_ID = ? AND TAG_TYPE = 'SERIES'",
            (tag_id,),
        ).fetchone()

        if row is None:
            extra = {
                "title": series_name,
                "titles": [
                    {
                        "display": series_name,
                        "collation": series_name,
                        "language": "en",
                        "pronunciation": series_name,
                    }
                ],
                "credit": "",
                "credits": [],
                "total_member_count": count + 1,
                "title_pronunciation": series_name,
                "thumbnail_location": "",
                "cover_aspect_ratio": 0,
                "sync_counter": 0,
            }
            conn.execute(
                """
                INSERT INTO Tags (
                    TAG_ID, TAG_TYPE, TAG_NAME, SYNC_STATE, DELETE_STATE,
                    ADDITIONAL_DATA, ROW_REVISION
                ) VALUES (?, 'SERIES', ?, 'SYNCED', '', ?, 1)
                """,
                (
                    tag_id,
                    series_name,
                    json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            row = conn.execute(
                "SELECT TAG_INTERNAL_ID, ADDITIONAL_DATA FROM Tags WHERE TAG_ID = ?",
                (tag_id,),
            ).fetchone()
            return row, True

        return row, False

    def _sync_series(self, conn, series, calibre_db, stats):
        node_by_asset, by_identifier, by_title = self._load_node_index(conn)

        for calibre_series in series:
            ordered_books = sorted(
                (calibre_db.get_metadata(book_id) for book_id in calibre_series.id_set),
                key=lambda book: book.get("series_index", 0),
            )
            series_name = calibre_series.name
            series_cde_key = _make_series_cde_key(series_name)
            tag_id = series_cde_key + "!!series"

            desired = {}
            unmatched = []
            for book in ordered_books:
                asset_id = self._match_book(
                    book, node_by_asset, by_identifier, by_title
                )
                if asset_id is None:
                    unmatched.append(book.title)
                    stats["unmatched"] += 1
                    print(f"  [MISSING] {book.title}")
                    continue

                index = float(book.get("series_index", 1))
                position = index - 1
                if asset_id in desired:
                    raise RuntimeError(
                        f"Duplicate Kindle book in series: {book.title} ({asset_id})"
                    )
                desired[asset_id] = {
                    "position": position,
                    "position_label": (
                        str(int(index)) if index.is_integer() else str(index)
                    ),
                    "title": book.title,
                }

            tag, tag_created = self._ensure_tag(conn, tag_id, series_name, len(desired))
            if tag_created:
                stats["added"] += 1
            tag_internal_id = tag["TAG_INTERNAL_ID"]
            current_rows = conn.execute(
                """
                SELECT ASSET_ID, ASSET_INTERNAL_ID, POSITION
                FROM ContentTag
                WHERE TAG_INTERNAL_ID = ? AND TAG_TYPE = 'SERIES'
                """,
                (tag_internal_id,),
            ).fetchall()
            current = {row["ASSET_ID"]: row for row in current_rows}

            if not unmatched:
                for asset_id, row in current.items():
                    if asset_id not in desired:
                        conn.execute(
                            "DELETE FROM ContentTag "
                            "WHERE TAG_INTERNAL_ID = ? AND ASSET_INTERNAL_ID = ?",
                            (tag_internal_id, row["ASSET_INTERNAL_ID"]),
                        )
                        stats["removed"] += 1

            for asset_id, item in desired.items():
                node = node_by_asset[asset_id]
                old = current.get(asset_id)
                if old is None:
                    stats["added"] += 1
                elif float(old["POSITION"] or 0) != float(item["position"]):
                    stats["updated"] += 1

                conn.execute(
                    """
                    INSERT INTO ContentTag (
                        TAG_INTERNAL_ID, ASSET_INTERNAL_ID, TAG_ID, ASSET_ID,
                        SYNC_STATE, DELETE_STATE, POSITION, ADDITIONAL_DATA,
                        ASSET_TYPE, TAG_TYPE
                    ) VALUES (?, ?, ?, ?, 'SYNCED', '', ?, '{"ordered":true}', ?, 'SERIES')
                    ON CONFLICT(TAG_INTERNAL_ID, ASSET_INTERNAL_ID) DO UPDATE SET
                        TAG_ID = excluded.TAG_ID,
                        ASSET_ID = excluded.ASSET_ID,
                        SYNC_STATE = 'SYNCED',
                        DELETE_STATE = '',
                        POSITION = excluded.POSITION,
                        ADDITIONAL_DATA = excluded.ADDITIONAL_DATA,
                        ASSET_TYPE = excluded.ASSET_TYPE,
                        TAG_TYPE = 'SERIES'
                    """,
                    (
                        tag_internal_id,
                        node["INTERNAL_ID"],
                        tag_id,
                        asset_id,
                        item["position"],
                        node["TYPE"] or "BOOK",
                    ),
                )

            relation_count = conn.execute(
                "SELECT COUNT(*) FROM ContentTag WHERE TAG_INTERNAL_ID = ?",
                (tag_internal_id,),
            ).fetchone()[0]
            tag_data = self._json_or(tag["ADDITIONAL_DATA"], {})
            if tag_data.get("total_member_count") != relation_count + 1:
                stats["updated"] += 1
            tag_data["total_member_count"] = relation_count + 1
            conn.execute(
                """
                UPDATE Tags
                SET CHILDREN_COUNT = ?, ADDITIONAL_DATA = ?,
                    ROW_REVISION = COALESCE(ROW_REVISION, 0) + 1
                WHERE TAG_INTERNAL_ID = ?
                """,
                (
                    relation_count,
                    json.dumps(tag_data, ensure_ascii=False, separators=(",", ":")),
                    tag_internal_id,
                ),
            )

            if relation_count >= 2:
                cursor = conn.execute(
                    """
                    UPDATE Nodes
                    SET VISIBILITY_WITH_GROUPING =
                            COALESCE(VISIBILITY_WITH_GROUPING, 0) | 1,
                        ROW_REVISION = COALESCE(ROW_REVISION, 0) + 1
                    WHERE INTERNAL_ID IN (
                        SELECT ASSET_INTERNAL_ID FROM ContentTag
                        WHERE TAG_INTERNAL_ID = ? AND TAG_TYPE = 'SERIES'
                    )
                    AND (COALESCE(VISIBILITY_WITH_GROUPING, 0) & 1) = 0
                    """,
                    (tag_internal_id,),
                )
                stats["updated"] += max(cursor.rowcount, 0)

            print(
                f"  {series_name}: +{stats['added']} / "
                f"~{stats['updated']} / -{stats['removed']}"
            )
            if unmatched:
                print(
                    f"  Skipped removals for {series_name}; "
                    f"{len(unmatched)} Calibre books did not match asset nodes."
                )

    def run(self):
        self._prepare_runtime_libraries()
        from .ssh_client import KindleSSHClient

        dialog = KindleSSHDialog(self.gui)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        client = KindleSSHClient()
        client.connect(**dialog.get_credentials())
        if not client.is_connected():
            info_dialog(
                self.gui,
                "Connection error",
                "Unable to connect to the Kindle. Check the IP, SSH port, and credentials.",
                show=True,
            )
            return

        calibre_db = self.gui.current_db.new_api
        series = calibre_db.get_categories().get("series", [])
        local_path = Path(tempfile.gettempdir()) / "ksdk.asset.db"
        remote_backup = "/var/local/ksdk.asset.db.asset-only.bk"
        remote_tmp = "/var/local/ksdk.asset.db.asset-only.tmp"

        # Create a consistent SQLite snapshot. This file is the only database
        # downloaded, opened, or uploaded by this action.
        client.execute(
            f"sqlite3 /var/local/ksdk.asset.db \".backup '{remote_backup}'\""
        )
        client.download(remote_backup, local_path)

        stats = {"added": 0, "updated": 0, "removed": 0, "unmatched": 0}
        conn = self._asset_connection(local_path)
        try:
            conn.execute("BEGIN")
            self._sync_series(conn, series, calibre_db, stats)
            conn.commit()
        except Exception as err:
            conn.rollback()
            info_dialog(self.gui, "Synchronization failed", str(err), show=True)
            raise
        finally:
            conn.close()

        if any(stats[key] for key in ("added", "updated", "removed")):
            client.upload(local_path, remote_tmp)
            stopped = False
            try:
                client.execute("stop kppmainapp")
                stopped = True
                client.execute(
                    "rm -f /var/local/ksdk.asset.db-wal "
                    "/var/local/ksdk.asset.db-shm && "
                    f"mv -f {remote_tmp} /var/local/ksdk.asset.db"
                )
            except Exception:
                if stopped:
                    try:
                        client.execute(
                            "rm -f /var/local/ksdk.asset.db-wal "
                            "/var/local/ksdk.asset.db-shm && "
                            f"cp -f {remote_backup} /var/local/ksdk.asset.db"
                        )
                    except Exception:
                        pass
                raise
            finally:
                if stopped:
                    try:
                        client.execute("start kppmainapp")
                    except Exception:
                        pass

        info_dialog(
            self.gui,
            "Asset database synchronization completed",
            f"Relations added:   {stats['added']}\n"
            f"Relations updated: {stats['updated']}\n"
            f"Relations removed: {stats['removed']}\n"
            f"Books unmatched:   {stats['unmatched']}\n",
            show=True,
        )
