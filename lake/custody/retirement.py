from dataclasses import dataclass

from lake import require_name, transaction
from lake.custody.inspection import InspectionRecord, read_pack_slice, validate_inspection
from lake.custody.pack_store import PackStore
from lake.custody.storage import VolumeStorage
from lake.custody.volumes import VolumeRef
from lake.publication.daybook_store import DaybookStore


@dataclass(frozen=True)
class PullPermit:
    pack_id: str


@dataclass(frozen=True)
class PullBlocked:
    blocking_volume_ids: tuple[str, ...]


def retire_volume(db, volume_id: str, record: InspectionRecord) -> VolumeRef:
    require_name(volume_id, 'volume identity')
    storage = VolumeStorage(db)
    with transaction(db, write=True):
        meta = storage.get_volume(volume_id)
        if meta is None:
            raise KeyError(volume_id)
        ref = VolumeRef(*meta)
        validate_inspection(storage, ref, record)
        storage.delete_hot(volume_id)
        return ref


def _manifest_volumes(store: PackStore, pack_id: str) -> tuple[str, ...]:
    """Distinct volume ids in first-appearance order of the admitted manifest."""
    return tuple(dict.fromkeys(row[0] for row in store.get_slices(pack_id)))


def _covered_elsewhere(store: PackStore, media, volume_id: str,
                       pack_id: str) -> bool:
    """Try every admitted, unpermitted slice outside the pack being authorized."""
    storage = VolumeStorage(store.db)
    meta = storage.get_volume(volume_id)
    if meta is None:
        return False
    ref = VolumeRef(*meta)
    for candidate_pack, offset, byte_length, _digest in store.candidate_slices(
            volume_id, pack_id):
        if read_pack_slice(media, candidate_pack, offset, byte_length, ref) is not None:
            return True
    return False


def authorize_pull(db, media, pack_id: str) -> PullPermit | PullBlocked:
    require_name(pack_id, 'pack id')
    store = PackStore(db)
    books = DaybookStore(db)
    with transaction(db, write=True):
        if not store.pack_exists(pack_id):
            raise KeyError(pack_id)
        if store.has_permit(pack_id):
            return PullPermit(pack_id)

        blocked = []
        for volume_id in _manifest_volumes(store, pack_id):
            if not books.references_volume(volume_id):
                continue
            if not _covered_elsewhere(store, media, volume_id, pack_id):
                blocked.append(volume_id)
        if blocked:
            return PullBlocked(tuple(blocked))

        store.issue_permit(pack_id)
        return PullPermit(pack_id)
