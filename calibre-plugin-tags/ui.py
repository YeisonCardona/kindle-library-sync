"""Calibre action for syncing Calibre tags into Kindle ksdk.asset.db only."""

import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from pathlib import Path

from calibre.gui2 import info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import (
    QCollator,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QLocale,
    QPixmap,
    QVBoxLayout,
)


def _normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _json_or(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _asset_connection(path):
    if not path.exists():
        raise FileNotFoundError(f"No existe la base de datos: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.create_collation("icu", lambda a, b: (a > b) - (a < b))
    try:
        conn.execute(
            "SELECT TITLE_COLLATION FROM Nodes ORDER BY TITLE_COLLATION COLLATE icu LIMIT 0"
        )
    except sqlite3.OperationalError as err:
        if "collation" not in str(err).lower():
            conn.close()
            raise
        collator = QCollator(QLocale())
        conn.create_collation("icu", lambda a, b: collator.compare(a or "", b or ""))
    return conn


def _book_identifiers(book):
    ids = getattr(book, "identifiers", None)
    if callable(ids):
        ids = ids()
    if not isinstance(ids, dict):
        try:
            ids = book.get("identifiers", {})
        except Exception:
            ids = {}
    return {_normalize(v) for v in (ids or {}).values() if v}


def _load_books(conn):
    nodes, by_id, by_title = {}, {}, {}
    rows = conn.execute(
        "SELECT INTERNAL_ID,INTERNAL_ID,ASSET_ID,ASIN,TITLE,TITLE_COLLATION,"
        "ADDITIONAL_DATA,TYPE FROM Nodes WHERE TYPE='BOOK' AND ASSET_ID LIKE '%!!EBOK'"
    ).fetchall()
    for row in rows:
        asset_id = row["ASSET_ID"]
        nodes[asset_id] = row
        for value in (asset_id, asset_id.rsplit("!!", 1)[0], row["ASIN"]):
            key = _normalize(value)
            if key:
                by_id.setdefault(key, set()).add(asset_id)
        titles = {row["TITLE"], row["TITLE_COLLATION"]}
        extra = _json_or(row["ADDITIONAL_DATA"], {})
        if isinstance(extra, dict):
            titles.update(
                extra.get(k) for k in ("title", "title_collation", "titleCollation")
            )
            for item in extra.get("titles", []) or []:
                if isinstance(item, dict):
                    titles.update(
                        item.get(k) for k in ("display", "collation", "title")
                    )
        for title in titles:
            key = _normalize(title)
            if key:
                by_title.setdefault(key, set()).add(asset_id)
    return nodes, by_id, by_title


def _match_book(book, index):
    nodes, by_id, by_title = index
    matches = set()
    for identifier in _book_identifiers(book):
        matches.update(by_id.get(identifier, ()))
    if len(matches) > 1:
        raise ValueError(f"Identificadores ambiguos para «{book.title}»")
    if matches:
        return nodes[next(iter(matches))]
    matches = by_title.get(_normalize(book.title), set())
    if len(matches) > 1:
        raise ValueError(f"Título ambiguo: «{book.title}»")
    return nodes[next(iter(matches))] if matches else None


def _tag_label(row):
    if row["TAG_NAME"]:
        return row["TAG_NAME"]
    data = _json_or(row["ADDITIONAL_DATA"], {})
    if isinstance(data, dict):
        if data.get("title"):
            return data["title"]
        titles = data.get("titles") or []
        if titles and isinstance(titles[0], dict):
            return titles[0].get("display") or titles[0].get("collation") or ""
    return ""


def _ensure_tag(conn, name, by_name, stats):
    key = _normalize(name)
    matches = by_name.get(key, [])
    if len(matches) > 1:
        raise ValueError(f"Hay varias etiquetas del Kindle llamadas «{name}»")
    if matches:
        return matches[0]
    tag_id = str(uuid.uuid4())
    data = {
        "title": name,
        "titles": [
            {
                "display": name,
                "collation": name,
                "language": "en",
                "pronunciation": name,
            }
        ],
        "credit": "",
        "credits": [],
    }
    conn.execute(
        "INSERT INTO Tags (TAG_ID,TAG_TYPE,TAG_NAME,SYNC_STATE,DELETE_STATE,ADDITIONAL_DATA,ROW_REVISION) "
        "VALUES (?,'USER_DEFINED_TAG',?,'SYNCED','',?,1)",
        (tag_id, name, json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    )
    row = conn.execute(
        "SELECT TAG_INTERNAL_ID,TAG_ID,TAG_TYPE,TAG_NAME,ADDITIONAL_DATA FROM Tags WHERE TAG_ID=?",
        (tag_id,),
    ).fetchone()
    by_name.setdefault(key, []).append(row)
    stats["tags_created"] += 1
    return row


def _sync_tags(conn, calibre_db):
    stats = {
        "tags_created": 0,
        "members_added": 0,
        "members_existing": 0,
        "unmatched_books": 0,
        "ambiguous_books": 0,
    }
    index = _load_books(conn)
    by_name = {}
    for row in conn.execute(
        "SELECT TAG_INTERNAL_ID,TAG_ID,TAG_TYPE,TAG_NAME,ADDITIONAL_DATA FROM Tags "
        "WHERE TAG_TYPE='USER_DEFINED_TAG' AND COALESCE(DELETE_STATE,'')=''"
    ):
        name = _tag_label(row)
        if _normalize(name):
            by_name.setdefault(_normalize(name), []).append(row)

    desired = {}
    for book_id in calibre_db.all_book_ids():
        book = calibre_db.get_metadata(book_id)
        try:
            node = _match_book(book, index)
        except ValueError:
            stats["ambiguous_books"] += 1
            continue
        if node is None:
            stats["unmatched_books"] += 1
            continue
        for raw in book.get("tags") or []:
            label = str(raw or "").strip()
            key = _normalize(label)
            if key:
                group = desired.setdefault(key, {"name": label, "members": {}})
                group["members"][node["ASSET_ID"]] = node

    for group in desired.values():
        tag = _ensure_tag(conn, group["name"], by_name, stats)
        for asset_id, node in group["members"].items():
            linked = conn.execute(
                "SELECT 1 FROM ContentTag WHERE TAG_INTERNAL_ID=? AND ASSET_INTERNAL_ID=? "
                "AND TAG_TYPE='USER_DEFINED_TAG' LIMIT 1",
                (tag["TAG_INTERNAL_ID"], node["INTERNAL_ID"]),
            ).fetchone()
            if linked:
                stats["members_existing"] += 1
                continue
            conn.execute(
                "INSERT INTO ContentTag (TAG_INTERNAL_ID,ASSET_INTERNAL_ID,TAG_ID,ASSET_ID,"
                "SYNC_STATE,DELETE_STATE,POSITION,ADDITIONAL_DATA,ASSET_TYPE,TAG_TYPE) "
                "VALUES (?,?,?,?, 'SYNCED','',0,'{\"ordered\":false}',?,'USER_DEFINED_TAG')",
                (
                    tag["TAG_INTERNAL_ID"],
                    node["INTERNAL_ID"],
                    tag["TAG_ID"],
                    asset_id,
                    node["TYPE"] or "BOOK",
                ),
            )
            stats["members_added"] += 1
    return stats


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


class SynchronizeTags(InterfaceAction):
    name = "Synchronize Tags"
    action_spec = (
        "Synchronize Tags",
        None,
        "Synchronize Calibre tags to Kindle",
        None,
    )

    def _prepare_runtime_libraries(self):
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        lib_dir = os.path.join(plugin_dir, "_tags_manager", "lib")
        if not os.path.isdir(lib_dir) and ".zip" in plugin_dir:
            zip_path = plugin_dir.split(".zip", 1)[0] + ".zip"
            target = os.path.join(os.path.dirname(zip_path), "_tags_manager")
            lib_dir = os.path.join(target, "lib")
            if not os.path.isdir(lib_dir) and os.path.exists(zip_path):
                os.makedirs(target, exist_ok=True)
                with zipfile.ZipFile(zip_path) as archive:
                    for filename in archive.namelist():
                        if filename.startswith("lib/"):
                            archive.extract(filename, target)
        if os.path.isdir(lib_dir) and lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)

    def genesis(self):
        resources = self.load_resources(["images/etiquetas.png"])

        pixmap = QPixmap()
        loaded = pixmap.loadFromData(resources["images/etiquetas.png"])

        if loaded:
            icon = QIcon(pixmap)
            self.qaction.setIcon(icon)

        self.qaction.triggered.connect(self.run)

    def run(self):
        self._prepare_runtime_libraries()
        from .ssh_client import KindleSSHClient

        dialog = KindleSSHDialog(self.gui)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        client = KindleSSHClient()
        local_path = Path(tempfile.gettempdir()) / (
            "ksdk.asset.tags." + uuid.uuid4().hex + ".db"
        )
        remote_backup = "/var/local/ksdk.asset.db.tagsync.bk"
        remote_tmp = "/var/local/ksdk.asset.db.tagsync.tmp"
        conn = None
        try:
            client.connect(**dialog.get_credentials())
            if not client.is_connected():
                raise RuntimeError("Unable to connect to the Kindle.")
            client.execute(
                f"sqlite3 /var/local/ksdk.asset.db \".backup '{remote_backup}'\""
            )
            client.download(remote_backup, local_path)
            conn = _asset_connection(local_path)
            conn.execute("BEGIN")
            stats = _sync_tags(conn, self.gui.current_db.new_api)
            conn.commit()
            conn.close()
            conn = None

            if stats["tags_created"] or stats["members_added"]:
                client.upload(local_path, remote_tmp)
                stopped = False
                try:
                    client.execute("stop kppmainapp")
                    stopped = True
                    client.execute(
                        "rm -f /var/local/ksdk.asset.db-wal /var/local/ksdk.asset.db-shm && "
                        f"mv -f {remote_tmp} /var/local/ksdk.asset.db"
                    )
                except Exception:
                    if stopped:
                        try:
                            client.execute(
                                f"rm -f /var/local/ksdk.asset.db-wal /var/local/ksdk.asset.db-shm && cp -f {remote_backup} /var/local/ksdk.asset.db"
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
                "Tag synchronization completed",
                f"Tags created: {stats['tags_created']}\n"
                f"Book-tag relations added: {stats['members_added']}\n"
                f"Relations already present: {stats['members_existing']}\n"
                f"Books not found: {stats['unmatched_books']}\n"
                f"Ambiguous books skipped: {stats['ambiguous_books']}\n"
                "Existing Kindle memberships were retained.",
                show=True,
            )
        except Exception as err:
            if conn is not None:
                conn.rollback()
            info_dialog(self.gui, "Tag synchronization failed", str(err), show=True)
        finally:
            if conn is not None:
                conn.close()
            try:
                local_path.unlink()
            except OSError:
                pass
