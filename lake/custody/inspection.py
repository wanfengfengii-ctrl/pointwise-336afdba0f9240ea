from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from lake import ConflictError, DataIntegrityError, require_name, transaction
from lake.custody.pack_store import PackStore
from lake.custody.storage import VolumeStorage
from lake.custody.volumes import VolumeRef, matches_volume


@dataclass(frozen=True)
class InspectionRecord:
    volume_id: str
    target: str
    byte_length: int
    digest: str


@dataclass(frozen=True)
class PackSlice:
    pack_id: str
    volume_id: str
    offset: int
    byte_length: int
    digest: str


def verify_cold_copy(storage: VolumeStorage, ref: VolumeRef, target: str) -> InspectionRecord:
    body = storage.get_cold_bytes(ref.volume_id, target)
    if not matches_volume(ref, body):
        raise DataIntegrityError('cold target has missing or mismatched bytes')
    return InspectionRecord(ref.volume_id, target, ref.byte_length, ref.digest)


def validate_inspection(storage: VolumeStorage, ref: VolumeRef, record: InspectionRecord):
    if not isinstance(record, InspectionRecord) or record.volume_id != ref.volume_id:
        raise DataIntegrityError('inspection belongs to a different volume')
    saved = storage.get_inspection(ref.volume_id, record.target)
    if saved is None or InspectionRecord(*saved) != record:
        raise DataIntegrityError('inspection record does not match the stored record')
    if verify_cold_copy(storage, ref, record.target) != record:
        raise DataIntegrityError('inspection differs from current verified evidence')


def normalize_slices(slices: Iterable) -> tuple[tuple[str, int, int], ...]:
    """Validate an ordered (volume_id, offset, byte_length) manifest."""
    if isinstance(slices, (str, bytes)) or not isinstance(slices, Iterable):
        raise ValueError('pack slices must be an ordered collection')
    normalized = []
    for item in slices:
        if isinstance(item, (str, bytes)) or not isinstance(item, Iterable):
            raise ValueError('each pack slice must be a volume, offset and length')
        parts = tuple(item)
        if len(parts) != 3:
            raise ValueError('each pack slice must be a volume, offset and length')
        volume_id, offset, byte_length = parts
        require_name(volume_id, 'volume identity')
        if type(offset) is not int or offset < 0:
            raise ValueError('slice offset must be a nonnegative integer')
        if type(byte_length) is not int or byte_length <= 0:
            raise ValueError('slice length must be a positive integer')
        normalized.append((volume_id, offset, byte_length))
    if not normalized:
        raise ValueError('a pack needs at least one slice')
    return tuple(normalized)


def read_volume_bytes(media, pack_id: str, offset: int, byte_length: int,
                      ref: VolumeRef) -> bytes | None:
    """Read one range from the physical pack and match it to the fixed volume."""
    body = media.read_slice(pack_id, offset, byte_length)
    if body is None or not isinstance(body, (bytes, bytearray)):
        return None
    body = bytes(body)
    if (len(body) != byte_length or len(body) != ref.byte_length
            or sha256(body).hexdigest() != ref.digest):
        return None
    return body


def read_pack_slice(media, pack_id: str, offset: int, byte_length: int,
                    ref: VolumeRef) -> PackSlice | None:
    if read_volume_bytes(media, pack_id, offset, byte_length, ref) is None:
        return None
    return PackSlice(pack_id, ref.volume_id, offset, byte_length, ref.digest)


def admit_pack(db, media, pack_id: str, slices) -> tuple[PackSlice, ...]:
    require_name(pack_id, 'pack id')
    requested = normalize_slices(slices)
    store = PackStore(db)
    storage = VolumeStorage(db)
    with transaction(db, write=True):
        if store.pack_exists(pack_id):
            saved = store.get_slices(pack_id)
            if tuple(item[:3] for item in saved) != requested:
                raise ConflictError('pack id already has a different slice manifest')
            return tuple(PackSlice(pack_id, *item) for item in saved)

        refs = {}
        for volume_id, _, _ in requested:
            if volume_id in refs:
                continue
            meta = storage.get_volume(volume_id)
            if meta is None:
                raise KeyError(volume_id)
            refs[volume_id] = VolumeRef(*meta)

        records = []
        for volume_id, offset, byte_length in requested:
            record = read_pack_slice(media, pack_id, offset, byte_length, refs[volume_id])
            if record is None:
                raise DataIntegrityError('pack has a missing or mismatched slice')
            records.append(record)

        store.insert_pack(pack_id, [(record.volume_id, record.offset, record.byte_length,
                                     record.digest) for record in records])
        return tuple(records)
