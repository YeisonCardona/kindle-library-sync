"""Framework-independent helpers for reading and editing ksdk.asset.db."""

import json
import re
import sqlite3
import tempfile
import uuid
import unicodedata
from pathlib import Path

REMOTE_ASSET_DB = "/var/local/ksdk.asset.db"


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value.casefold()).strip()


def json_or(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def open_asset_db(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Asset database not found: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.create_collation("icu", lambda left, right: (left > right) - (left < right))
    return conn


def _book_identifiers(identifiers):
    if not isinstance(identifiers, dict):
        return set()
    return {normalize(value) for value in (identifiers or {}).values() if value}


class BookIndex:
    """Index Kindle book nodes and match external book metadata to them."""

    def __init__(self, conn):
        self.nodes = {}
        self.by_identifier = {}
        self.by_title = {}
        rows = conn.execute(
            "SELECT INTERNAL_ID,ASSET_ID,ASIN,TITLE,TITLE_COLLATION,ADDITIONAL_DATA,TYPE "
            "FROM Nodes WHERE TYPE='BOOK' AND ASSET_ID LIKE '%!!EBOK'"
        )
        for row in rows:
            asset_id = row["ASSET_ID"]
            self.nodes[asset_id] = row
            asin = row["ASIN"] or asset_id.rsplit("!!", 1)[0]
            for value in (asset_id, asset_id.rsplit("!!", 1)[0], asin):
                key = normalize(value)
                if key:
                    self.by_identifier.setdefault(key, set()).add(asset_id)
            titles = {row["TITLE"], row["TITLE_COLLATION"]}
            extra = json_or(row["ADDITIONAL_DATA"], {})
            if isinstance(extra, dict):
                titles.update(
                    extra.get(key)
                    for key in ("title", "title_collation", "titleCollation")
                )
                for item in extra.get("titles", []) or []:
                    if isinstance(item, dict):
                        titles.update(
                            item.get(key) for key in ("display", "collation", "title")
                        )
            for title in titles:
                key = normalize(title)
                if key:
                    self.by_title.setdefault(key, set()).add(asset_id)

    def match(self, title, identifiers=None):
        matches = set()
        for identifier in _book_identifiers(identifiers):
            matches.update(self.by_identifier.get(identifier, ()))
        if len(matches) > 1:
            raise ValueError(f"Ambiguous identifier match for book: {title}")
        if matches:
            return self.nodes[next(iter(matches))]
        matches = self.by_title.get(normalize(title), set())
        if len(matches) > 1:
            raise ValueError(f"Ambiguous title match: {title}")
        return self.nodes[next(iter(matches))] if matches else None


def tag_display_name(row):
    if row["TAG_NAME"]:
        return row["TAG_NAME"]
    extra = json_or(row["ADDITIONAL_DATA"], {})
    if isinstance(extra, dict):
        if extra.get("title"):
            return extra["title"]
        titles = extra.get("titles") or []
        if titles and isinstance(titles[0], dict):
            return titles[0].get("display") or titles[0].get("collation") or ""
    return ""


def ensure_tag(conn, tag_id, name, tag_type, extra, existing=None):
    """Return (tag row, created); IDs are stable and tag type is explicit."""
    if existing is None:
        row = conn.execute(
            "SELECT TAG_INTERNAL_ID,TAG_ID,TAG_TYPE,TAG_NAME,ADDITIONAL_DATA "
            "FROM Tags WHERE TAG_ID=? AND TAG_TYPE=?",
            (tag_id, tag_type),
        ).fetchone()
    else:
        row = existing
    if row:
        return row, False
    conn.execute(
        "INSERT INTO Tags (TAG_ID,TAG_TYPE,TAG_NAME,SYNC_STATE,DELETE_STATE,"
        "ADDITIONAL_DATA,ROW_REVISION) VALUES (?,?,?,'SYNCED','',?,1)",
        (
            tag_id,
            tag_type,
            name,
            json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
        ),
    )
    row = conn.execute(
        "SELECT TAG_INTERNAL_ID,TAG_ID,TAG_TYPE,TAG_NAME,ADDITIONAL_DATA "
        "FROM Tags WHERE TAG_ID=? AND TAG_TYPE=?",
        (tag_id, tag_type),
    ).fetchone()
    return row, True


def add_content_tag(conn, tag, node, position=0, ordered=False, tag_type=None):
    """Insert one book/tag relation unless the same relation already exists."""
    tag_type = tag_type or tag["TAG_TYPE"]
    exists = conn.execute(
        "SELECT 1 FROM ContentTag WHERE TAG_INTERNAL_ID=? AND ASSET_INTERNAL_ID=? "
        "AND TAG_TYPE=? LIMIT 1",
        (tag["TAG_INTERNAL_ID"], node["INTERNAL_ID"], tag_type),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO ContentTag (TAG_INTERNAL_ID,ASSET_INTERNAL_ID,TAG_ID,ASSET_ID,"
        "SYNC_STATE,DELETE_STATE,POSITION,ADDITIONAL_DATA,ASSET_TYPE,TAG_TYPE) "
        "VALUES (?,?,?,?, 'SYNCED','',?,?,?,?)",
        (
            tag["TAG_INTERNAL_ID"],
            node["INTERNAL_ID"],
            tag["TAG_ID"],
            node["ASSET_ID"],
            position,
            json.dumps({"ordered": bool(ordered)}, separators=(",", ":")),
            node["TYPE"] or "BOOK",
            tag_type,
        ),
    )
    return True


def sync_remote_asset_db(client, callback, source_data, backup_suffix, restart):
    """Apply callback to a local snapshot and replace the remote asset DB.

    The caller owns connection setup, dialogs, and user-facing error reporting.
    """
    local_path = Path(tempfile.gettempdir()) / (
        "ksdk.asset." + uuid.uuid4().hex + ".db"
    )
    remote_backup = REMOTE_ASSET_DB + "." + backup_suffix + ".bk"
    remote_tmp = REMOTE_ASSET_DB + "." + backup_suffix + ".tmp"
    conn = None
    stopped = False
    try:
        client.execute(f"sqlite3 {REMOTE_ASSET_DB} \".backup '{remote_backup}'\"")
        client.download(remote_backup, local_path)
        conn = open_asset_db(local_path)
        conn.execute("BEGIN")
        stats = callback(conn, source_data)
        conn.commit()
        conn.close()
        conn = None
        if stats.get("changed", True):
            client.upload(local_path, remote_tmp)
            if restart:
                client.execute("stop kppmainapp")
            stopped = True
            client.execute(
                f"rm -f {REMOTE_ASSET_DB}-wal {REMOTE_ASSET_DB}-shm && "
                f"mv -f {remote_tmp} {REMOTE_ASSET_DB}"
            )
        return stats
    except Exception:
        if conn is not None:
            conn.rollback()
        if stopped:
            try:
                client.execute(
                    f"rm -f {REMOTE_ASSET_DB}-wal {REMOTE_ASSET_DB}-shm && "
                    f"cp -f {remote_backup} {REMOTE_ASSET_DB}"
                )
            except Exception:
                pass
        raise
    finally:
        if stopped:
            try:
                if restart:
                    client.execute("start kppmainapp")
            except Exception:
                pass
        if conn is not None:
            conn.close()
        try:
            local_path.unlink()
        except OSError:
            pass
