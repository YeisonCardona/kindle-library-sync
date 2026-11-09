"""Calibre action for syncing tags into Kindle's ksdk.asset.db."""

import uuid

from calibre.gui2 import info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QDialog, QIcon, QPixmap

from .asset_db import (
    BookIndex,
    add_content_tag,
    ensure_tag,
    normalize,
    sync_remote_asset_db,
    tag_display_name,
)
from .ui_dialogs import (
    KindleSSHDialog,
    calibre_book_identity,
    prepare_runtime_libraries,
)


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


class SynchronizeTags(InterfaceAction):
    name = "Synchronize Tags"
    action_spec = ("Synchronize Tags", None, "Synchronize Calibre tags to Kindle", None)

    def genesis(self):
        resources = self.load_resources(["images/etiquetas.png"])
        pixmap = QPixmap()
        if pixmap.loadFromData(resources["images/etiquetas.png"]):
            self.qaction.setIcon(QIcon(pixmap))
        self.qaction.triggered.connect(self.run)

    def run(self):
        prepare_runtime_libraries(__file__, "_tags_manager")
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
                client,
                _sync_tags,
                self.gui.current_db.new_api,
                "tagsync",
                dialog.restart_requested(),
            )
            info_dialog(
                self.gui,
                stats.pop("title", "Tag synchronization completed"),
                stats.pop("message", str(stats)),
                show=True,
            )
        except Exception as err:
            info_dialog(self.gui, "Tag synchronization failed", str(err), show=True)
