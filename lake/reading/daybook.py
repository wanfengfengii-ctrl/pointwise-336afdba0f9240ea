from dataclasses import dataclass, field

from lake import require_name, transaction
from lake.custody.inspection import PackSlice, read_volume_bytes
from lake.custody.pack_store import PackStore
from lake.publication.daybook_store import DaybookStore
from lake.publication.manifests import DayManifest
from lake.publication.receipts import PublicationReceipt
from lake.quality.station_totals import DayTotals
from lake.reading.coverage import VolumeCoverage, collect_coverage
from lake.reading.details import ReadDetail, assemble_details
from lake.reading.reconcile import ReadResult, reconcile_daybook


@dataclass(frozen=True)
class ReadUnavailable:
    number: str
    missing_volume_ids: tuple[str, ...]
    details: tuple[ReadDetail, ...] = field(default=(), init=False)


@dataclass(frozen=True)
class ColdReadResult:
    manifest: DayManifest
    totals: DayTotals
    receipt: PublicationReceipt
    details: tuple[ReadDetail, ...]
    evidence: tuple[PackSlice, ...]


def read_day(db, number: str) -> ReadResult | ReadUnavailable:
    require_name(number, 'daybook number')
    with transaction(db):
        book = DaybookStore(db).get(number)
        if book is None:
            raise KeyError(number)
        coverage = collect_coverage(db, book.manifest)
        if coverage.missing_volume_ids:
            return ReadUnavailable(number, coverage.missing_volume_ids)
        details = assemble_details(book.manifest, coverage)
        return reconcile_daybook(book, details)


def collect_pack_evidence(store: PackStore, media, manifest: DayManifest):
    """Read each fixed volume from admitted packs within the caller's snapshot."""
    evidence = []
    bodies = []
    missing = {}
    for entry in manifest.volumes:
        ref = entry.volume
        found = None
        for pack_id, offset, byte_length, digest in store.cold_candidates(ref.volume_id):
            if digest != ref.digest:
                continue
            body = read_volume_bytes(media, pack_id, offset, byte_length, ref)
            if body is not None:
                found = (PackSlice(pack_id, ref.volume_id, offset, byte_length, ref.digest), body)
                break
        if found is None:
            missing[ref.volume_id] = None
        else:
            evidence.append(found[0])
            bodies.append(found[1])
    return tuple(evidence), tuple(bodies), tuple(missing)


def read_cold_day(db, media, number: str) -> ColdReadResult | ReadUnavailable:
    require_name(number, 'daybook number')
    with transaction(db):
        book = DaybookStore(db).get(number)
        if book is None:
            raise KeyError(number)
        evidence, bodies, missing = collect_pack_evidence(PackStore(db), media, book.manifest)
        if missing:
            return ReadUnavailable(number, missing)
        coverage = VolumeCoverage(bodies, ())
        details = assemble_details(book.manifest, coverage)
        result = reconcile_daybook(book, details)
        return ColdReadResult(result.manifest, result.totals, result.receipt,
                              result.details, evidence)
