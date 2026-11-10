"""Calibre action for syncing series and tags into Kindle's ksdk.asset.db."""

import json
import uuid

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog
from qt.core import QDialog, QIcon, QPixmap

from .asset_db import (
    BookIndex,
    ensure_tag,
    json_or,
    sync_remote_asset_db,
    normalize,
    tag_display_name,
    add_content_tag,
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


def _sync_tags(conn, calibre_db):
    stats = {
        "tags_created": 0,
        "members_added": 0,
        "members_existing": 0,
        "unmatched_books": 0,
        "ambiguous_books": 0,
    }
    book_index = BookIndex(conn)
    tags_by_name = {}
    for row in conn.execute(
        "SELECT TAG_INTERNAL_ID,TAG_ID,TAG_TYPE,TAG_NAME,ADDITIONAL_DATA FROM Tags "
        "WHERE TAG_TYPE='USER_DEFINED_TAG' AND COALESCE(DELETE_STATE,'')=''"
    ):
        key = normalize(tag_display_name(row))
        if key:
            tags_by_name.setdefault(key, []).append(row)

    desired = {}
    for book_id in calibre_db.all_book_ids():
        book = calibre_db.get_metadata(book_id)
        try:
            node = book_index.match(*calibre_book_identity(book))
        except ValueError:
            stats["ambiguous_books"] += 1
            continue
        if node is None:
            stats["unmatched_books"] += 1
            continue
        for raw_tag in book.get("tags") or []:
            name = str(raw_tag or "").strip()
            key = normalize(name)
            if key:
                group = desired.setdefault(key, {"name": name, "members": {}})
                group["members"][node["ASSET_ID"]] = node

    for key, group in desired.items():
        matches = tags_by_name.get(key, [])
        if len(matches) > 1:
            raise ValueError(f"Duplicate Kindle user tags named: {group['name']}")
        if matches:
            tag = matches[0]
        else:
            extra = {
                "title": group["name"],
                "titles": [
                    {
                        "display": group["name"],
                        "collation": group["name"],
                        "language": "en",
                        "pronunciation": group["name"],
                    }
                ],
                "credit": "",
                "credits": [],
            }
            tag, created = ensure_tag(
                conn, str(uuid.uuid4()), group["name"], "USER_DEFINED_TAG", extra
            )
            tags_by_name.setdefault(key, []).append(tag)
            stats["tags_created"] += int(created)

        for node in group["members"].values():
            if add_content_tag(conn, tag, node, ordered=False):
                stats["members_added"] += 1
            else:
                stats["members_existing"] += 1

    stats["changed"] = bool(stats["tags_created"] or stats["members_added"])
    stats["title"] = "Tag synchronization completed"
    stats["message"] = (
        f"Tags created: {stats['tags_created']}\n"
        f"Book-tag relations added: {stats['members_added']}\n"
        f"Relations already present: {stats['members_existing']}\n"
        f"Books not found: {stats['unmatched_books']}\n"
        f"Ambiguous books skipped: {stats['ambiguous_books']}\n"
        "Existing Kindle memberships were retained."
    )
    return stats


class SynchronizeSeriesAndTags(InterfaceAction):
    """Synchronize Calibre series and tags into ksdk.asset.db; never opens cc.db."""

    name = "Synchronize Series and Tags"
    action_spec = (
        "Synchronize Series and Tags",
        None,
        "Synchronize Series and Tags",
        None,
    )

    def genesis(self):
        resources = self.load_resources(["images/libros.png"])
        pixmap = QPixmap()
        if pixmap.loadFromData(resources["images/libros.png"]):
            self.qaction.setIcon(QIcon(pixmap))
        self.qaction.triggered.connect(self.run)

    def run(self):
        prepare_runtime_libraries(__file__, "_sync_manager")
        dialog = KindleSSHDialog(self.gui)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        from .ssh_client import KindleSSHClient

        client = KindleSSHClient()
        try:
            client.connect(**dialog.get_credentials())
            if not client.is_connected():
                raise RuntimeError("Unable to connect to the Kindle.")

            if dialog.sync_options()["series"]:
                stats_series = sync_remote_asset_db(
                    client=client,
                    callback=_sync_series,
                    source_data=self.gui.current_db.new_api,
                    backup_suffix="backup_series",
                    restart=False,  # NO restart
                )

            if dialog.sync_options()["tags"]:
                stats_tags = sync_remote_asset_db(
                    client=client,
                    callback=_sync_tags,
                    source_data=self.gui.current_db.new_api,
                    backup_suffix="backup_series_tags",
                    restart=dialog.restart_requested(),
                )

            info_dialog(
                self.gui,
                stats_series.pop("title", "Asset database synchronization completed"),
                f'Series:\n{stats_series.pop("message", str(stats_series))}\n\n'
                f'Tags:\n{stats_tags.pop("message", str(stats_tags))}',
                show=True,
            )
        except Exception as err:
            info_dialog(self.gui, "Synchronization failed", str(err), show=True)
