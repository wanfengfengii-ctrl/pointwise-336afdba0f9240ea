from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3

from lake import ConflictError, require_name
from lake.custody.cold_copy import archive_volume
from lake.custody.retirement import retire_volume
from lake.custody.storage import VolumeStorage
from lake.custody.volumes import VolumeRef
from lake.intake.batches import receive_batch
from lake.publication.daybooks import publish_day
from lake.quality.rules import register_ruleset


class PackMedia:
    "External media fixture independent of the SQLite connection."

    def __init__(self):
        self._mounted = {}
        self._used_ids = set()

    def mount(self, pack_id, body: bytes):
        require_name(pack_id, "pack id")
        if not isinstance(body, bytes):
            raise TypeError("media body must be bytes")
        if pack_id in self._used_ids:
            raise ConflictError("physical pack id is already used")
        self._used_ids.add(pack_id)
        self._mounted[pack_id] = body

    def read_slice(self, pack_id, offset: int, byte_length: int) -> bytes | None:
        if type(offset) is not int or type(byte_length) is not int or min(offset, byte_length) < 0:
            raise ValueError("invalid byte range")
        body = self._mounted.get(pack_id)
        return None if body is None else body[offset:offset + byte_length]

    def pull(self, pack_id):
        self._mounted.pop(pack_id, None)

    def corrupt_slice(self, pack_id, offset: int, replacement: bytes):
        if not isinstance(replacement, bytes):
            raise TypeError("replacement must be bytes")
        body = self._mounted[pack_id]
        end = offset + len(replacement)
        if offset < 0 or end > len(body):
            raise ValueError("fault must stay within the physical pack")
        self._mounted[pack_id] = body[:offset] + replacement + body[end:]


@dataclass(frozen=True)
class PackSample:
    media: PackMedia
    volumes: dict[str, VolumeRef]
    layouts: dict[str, tuple[tuple[str, int, int], ...]]


def _mount(media, pack_id, entries):
    payload = bytearray(pack_id.encode("ascii") + b"|")
    layout = []
    for ref, body in entries:
        layout.append((ref.volume_id, len(payload), len(body)))
        payload.extend(body)
    media.mount(pack_id, bytes(payload))
    return tuple(layout)


def make_pack_sample(db) -> PackSample:
    day = "2026-10-02"
    rule = register_ruleset(db, "pack-20", 0, 20)
    storage = VolumeStorage(db)
    volumes = {}
    bodies = {}
    source_batches = {}
    for label, name, station, values in (
        ("X", "甲账", "东堤", [0, 7, None]),
        ("Y", "乙账", "南闸", [11, 25]),
        ("Z", "丙账", "东堤", [0, 7, None]),
    ):
        batch = receive_batch(db, station, day, label.lower(), values).identity
        source_batches[label] = batch
        book = publish_day(db, day, [batch], rule.revision, name)
        volumes[label] = book.manifest.volumes[0].volume
    publish_day(db, day, [source_batches["Y"], source_batches["X"]], rule.revision, "联账")
    for label, ref in volumes.items():
        record = archive_volume(db, ref.volume_id, "散冷柜")
        body = storage.get_cold_bytes(ref.volume_id, record.target)
        if body is None:
            raise AssertionError("V1 archive did not store a body")
        bodies[label] = body
        retire_volume(db, ref.volume_id, record)

    media = PackMedia()
    p = _mount(media, "P", [(volumes["X"], bodies["X"]), (volumes["Y"], bodies["Y"])])
    old_x = media.read_slice("P", *p[0][1:])
    old_y = media.read_slice("P", *p[1][1:])
    q = _mount(media, "Q", [(volumes["X"], old_x), (volumes["Z"], bodies["Z"])])
    s = _mount(media, "S", [(volumes["Y"], old_y)])
    return PackSample(media, volumes, {"P": p, "Q": q, "S": s})


@contextmanager
def fail_commit(db):
    "Deny SQLite COMMIT on this fixture connection and allow rollback."
    def authorize(action, first, _second, _database, _source):
        if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    try:
        yield
    finally:
        db.set_authorizer(None)
