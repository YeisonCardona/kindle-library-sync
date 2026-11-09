"""Calibre action for syncing series into Kindle's ksdk.asset.db."""

import json

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog
from qt.core import QDialog, QIcon, QPixmap

from .asset_db import (
    BookIndex,
    ensure_tag,
    json_or,
    sync_remote_asset_db,
)
from .ui_dialogs import (
    KindleSSHDialog,
    calibre_book_identity,
    prepare_runtime_libraries,
)


def _series_tag_data(name, member_count):
    return {
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
        "total_member_count": member_count,
        "title_pronunciation": name,
        "thumbnail_location": "",
        "cover_aspect_ratio": 0,
        "sync_counter": 0,
    }


def _series_tag_id(name):
    return "SL-" + name.upper().replace(" ", "-").replace("'", "") + "!!series"


def _sync_series(conn, calibre_db):
    stats = {"added": 0, "updated": 0, "removed": 0, "unmatched": 0}
    book_index = BookIndex(conn)

    for series in calibre_db.get_categories().get("series", []):
        ordered_books = sorted(
            (calibre_db.get_metadata(book_id) for book_id in series.id_set),
            key=lambda book: book.get("series_index", 0),
        )
        name = series.name
        tag_id = _series_tag_id(name)
        desired, unmatched = {}, []
        for book in ordered_books:
            try:
                node = book_index.match(*calibre_book_identity(book))
            except ValueError as err:
                raise RuntimeError(str(err)) from err
            if node is None:
                unmatched.append(book.title)
                stats["unmatched"] += 1
                continue
            index = float(book.get("series_index", 1))
            asset_id = node["ASSET_ID"]
            if asset_id in desired:
                raise RuntimeError(
                    f"Duplicate Kindle book in series {name}: {book.title}"
                )
            desired[asset_id] = {"node": node, "position": index - 1}

        tag, created = ensure_tag(
            conn, tag_id, name, "SERIES", _series_tag_data(name, len(desired))
        )
        if created:
            stats["added"] += 1
        tag_internal_id = tag["TAG_INTERNAL_ID"]
        current_rows = conn.execute(
            "SELECT ASSET_ID,ASSET_INTERNAL_ID,POSITION FROM ContentTag "
            "WHERE TAG_INTERNAL_ID=? AND TAG_TYPE='SERIES'",
            (tag_internal_id,),
        ).fetchall()
        current = {row["ASSET_ID"]: row for row in current_rows}

        if not unmatched:
            for asset_id, relation in current.items():
                if asset_id not in desired:
                    conn.execute(
                        "DELETE FROM ContentTag WHERE TAG_INTERNAL_ID=? AND ASSET_INTERNAL_ID=?",
                        (tag_internal_id, relation["ASSET_INTERNAL_ID"]),
                    )
                    stats["removed"] += 1

        for asset_id, item in desired.items():
            old = current.get(asset_id)
            position = item["position"]
            if old is None:
                stats["added"] += 1
            elif float(old["POSITION"] or 0) != position:
                stats["updated"] += 1
            node = item["node"]
            conn.execute(
                "INSERT INTO ContentTag (TAG_INTERNAL_ID,ASSET_INTERNAL_ID,TAG_ID,ASSET_ID,"
                "SYNC_STATE,DELETE_STATE,POSITION,ADDITIONAL_DATA,ASSET_TYPE,TAG_TYPE) "
                "VALUES (?,?,?,?, 'SYNCED','',?,'{\"ordered\":true}',?,'SERIES') "
                "ON CONFLICT(TAG_INTERNAL_ID,ASSET_INTERNAL_ID) DO UPDATE SET "
                "TAG_ID=excluded.TAG_ID,ASSET_ID=excluded.ASSET_ID,SYNC_STATE='SYNCED',"
                "DELETE_STATE='',POSITION=excluded.POSITION,"
                "ADDITIONAL_DATA=excluded.ADDITIONAL_DATA,ASSET_TYPE=excluded.ASSET_TYPE,"
                "TAG_TYPE='SERIES'",
                (
                    tag_internal_id,
                    node["INTERNAL_ID"],
                    tag_id,
                    asset_id,
                    position,
                    node["TYPE"] or "BOOK",
                ),
            )

        relation_count = conn.execute(
            "SELECT COUNT(*) FROM ContentTag WHERE TAG_INTERNAL_ID=? AND TAG_TYPE='SERIES'",
            (tag_internal_id,),
        ).fetchone()[0]
        tag_data = json_or(tag["ADDITIONAL_DATA"], {})
        if tag_data.get("total_member_count") != relation_count:
            stats["updated"] += 1
        tag_data["total_member_count"] = relation_count
        conn.execute(
            "UPDATE Tags SET CHILDREN_COUNT=?,ADDITIONAL_DATA=?,"
            "ROW_REVISION=COALESCE(ROW_REVISION,0)+1 WHERE TAG_INTERNAL_ID=?",
            (
                relation_count,
                json.dumps(tag_data, ensure_ascii=False, separators=(",", ":")),
                tag_internal_id,
            ),
        )
        if relation_count >= 2:
            cursor = conn.execute(
                "UPDATE Nodes SET VISIBILITY_WITH_GROUPING=COALESCE(VISIBILITY_WITH_GROUPING,0)|1,"
                "ROW_REVISION=COALESCE(ROW_REVISION,0)+1 WHERE INTERNAL_ID IN "
                "(SELECT ASSET_INTERNAL_ID FROM ContentTag WHERE TAG_INTERNAL_ID=? AND TAG_TYPE='SERIES') "
                "AND (COALESCE(VISIBILITY_WITH_GROUPING,0)&1)=0",
                (tag_internal_id,),
            )
            stats["updated"] += max(cursor.rowcount, 0)
        if unmatched:
            print(
                f"Skipped removals for {name}; {len(unmatched)} Calibre books did not match."
            )

    stats["changed"] = any(stats[key] for key in ("added", "updated", "removed"))
    stats["title"] = "Asset database synchronization completed"
    stats["message"] = (
        f"Relations added:   {stats['added']}\n"
        f"Relations updated: {stats['updated']}\n"
        f"Relations removed: {stats['removed']}\n"
        f"Books unmatched:   {stats['unmatched']}"
    )
    return stats


class SynchronizeSeries(InterfaceAction):
    """Synchronize Calibre series into ksdk.asset.db; never opens cc.db."""

    name = "Synchronize Series"
    action_spec = ("Synchronize Series", None, "Synchronize Series", None)

    def genesis(self):
        resources = self.load_resources(["images/libros.png"])
        pixmap = QPixmap()
        if pixmap.loadFromData(resources["images/libros.png"]):
            self.qaction.setIcon(QIcon(pixmap))
        self.qaction.triggered.connect(self.run)

    def run(self):
        prepare_runtime_libraries(__file__, "_series_manager")
        dialog = KindleSSHDialog(self.gui)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        from .ssh_client import KindleSSHClient

        client = KindleSSHClient()
        try:
            client.connect(**dialog.get_credentials())
            if not client.is_connected():
                raise RuntimeError("Unable to connect to the Kindle.")
            stats = sync_remote_asset_db(
                client, _sync_series, self.gui.current_db.new_api, "asset-only"
            )
            info_dialog(
                self.gui,
                stats.pop("title", "Asset database synchronization completed"),
                stats.pop("message", str(stats)),
                show=True,
            )
        except Exception as err:
            info_dialog(self.gui, "Series synchronization failed", str(err), show=True)
