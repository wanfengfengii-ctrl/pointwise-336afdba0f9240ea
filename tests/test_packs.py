import sqlite3

from lake import ConflictError, DataIntegrityError
from lake.custody.inspection import admit_pack
from lake.custody.pack_store import PackStore
from lake.custody.retirement import PullBlocked, PullPermit, authorize_pull
from lake.custody.storage import VolumeStorage
from lake.publication.daybooks import publish_day
from lake.reading.daybook import ColdReadResult, ReadUnavailable, read_cold_day
from tests.pack_fixture import fail_commit, make_pack_sample
from tests.support import DAY, LakeCase


def single_pack(media, pack_id, body: bytes):
    prefix = pack_id.encode('ascii') + b'|'
    media.mount(pack_id, prefix + body)
    return len(prefix)


class PackAdmissionTests(LakeCase):
    def test_admit_returns_ordered_records(self):
        sample = make_pack_sample(self.db)
        order = [('P', 0), ('P', 1)]
        slices = [(sample.layouts[pack][index][0], *sample.layouts[pack][index][1:])
                  for pack, index in order]
        admitted = admit_pack(self.db, sample.media, 'P', slices)
        self.assertEqual([(record.pack_id, record.volume_id, record.offset, record.byte_length)
                          for record in admitted],
                         [('P', sample.layouts['P'][0][0], *sample.layouts['P'][0][1:]),
                          ('P', sample.layouts['P'][1][0], *sample.layouts['P'][1][1:])])
        self.assertEqual([record.digest for record in admitted],
                         [sample.volumes['X'].digest, sample.volumes['Y'].digest])
        self.assertEqual([record.byte_length for record in admitted],
                         [sample.volumes['X'].byte_length, sample.volumes['Y'].byte_length])

    def test_same_manifest_reclaims_even_after_pull(self):
        sample = make_pack_sample(self.db)
        slices = [(volume_id, offset, length)
                  for volume_id, offset, length in sample.layouts['P']]
        admitted = admit_pack(self.db, sample.media, 'P', slices)
        self.db.close()
        sample.media.pull('P')
        self.reopen()
        self.assertEqual(admit_pack(self.db, sample.media, 'P', slices), admitted)
        self.assertEqual(PackStore(self.db).pack_exists('P'), True)
        with self.assertRaises(ConflictError):
            admit_pack(self.db, sample.media, 'P', slices[::-1])

    def test_any_bad_slice_rejects_the_whole_pack(self):
        sample = make_pack_sample(self.db)
        x_id = sample.volumes['X'].volume_id
        y_id = sample.volumes['Y'].volume_id
        good = sample.layouts['P'][0]
        bad_cases = [
            [(x_id, 0, good[2])],                          # wrong offset
            [(x_id, good[1], good[2] - 1)],                # short slice
            [(x_id, good[1] + sample.volumes['X'].byte_length + 1000, 1)],  # past the end
        ]
        for slices in bad_cases:
            with self.subTest(slices=slices):
                with self.assertRaises(DataIntegrityError):
                    admit_pack(self.db, sample.media, 'P', slices)
                self.assertFalse(PackStore(self.db).pack_exists('P'))
        with self.assertRaises(DataIntegrityError):
            admit_pack(self.db, sample.media, 'P',
                       [(x_id, *sample.layouts['P'][0][1:]), (y_id, 0, 3)])
        self.assertFalse(PackStore(self.db).pack_exists('P'))
        admitted = admit_pack(self.db, sample.media, 'P',
                              [(volume_id, offset, length)
                               for volume_id, offset, length in sample.layouts['P']])
        self.assertEqual(len(admitted), 2)

    def test_unknown_volume_and_bad_ranges(self):
        sample = make_pack_sample(self.db)
        x_id = sample.volumes['X'].volume_id
        with self.assertRaises(KeyError):
            admit_pack(self.db, sample.media, 'P', [('sha256:unknown', 0, 4)])
        for slices in ([], [(x_id, -1, 4)], [(x_id, 0, 0)], [(x_id, 0, True)],
                       [(x_id, True, 4)], [(x_id, 0)], [('', 0, 4)]):
            with self.subTest(slices=slices):
                with self.assertRaises(ValueError):
                    admit_pack(self.db, sample.media, 'P', slices)
        self.assertFalse(PackStore(self.db).pack_exists('P'))

    def test_admission_commit_failure_leaves_nothing(self):
        sample = make_pack_sample(self.db)
        slices = [(volume_id, offset, length)
                  for volume_id, offset, length in sample.layouts['P']]
        with fail_commit(self.db):
            with self.assertRaises(sqlite3.DatabaseError):
                admit_pack(self.db, sample.media, 'P', slices)
        self.assertFalse(self.db.in_transaction)
        self.reopen()
        self.assertFalse(PackStore(self.db).pack_exists('P'))
        self.assertEqual(len(admit_pack(self.db, sample.media, 'P', slices)), 2)


class PullPermitTests(LakeCase):
    def test_blocked_then_permitted_after_second_pack(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        admit_pack(self.db, media, 'P', [tuple(row) for row in sample.layouts['P']])
        x_id = sample.volumes['X'].volume_id
        y_id = sample.volumes['Y'].volume_id
        blocked = authorize_pull(self.db, media, 'P')
        self.assertEqual(blocked, PullBlocked((x_id, y_id)))
        admit_pack(self.db, media, 'Q', [tuple(row) for row in sample.layouts['Q']])
        blocked = authorize_pull(self.db, media, 'P')
        self.assertEqual(blocked, PullBlocked((y_id,)))
        admit_pack(self.db, media, 'S', [tuple(row) for row in sample.layouts['S']])
        permit = authorize_pull(self.db, media, 'P')
        self.assertEqual(permit, PullPermit('P'))
        self.assertEqual(authorize_pull(self.db, media, 'P'), permit)
        with self.assertRaises(KeyError):
            authorize_pull(self.db, media, 'unknown')

    def test_online_volume_does_not_cover_a_pack(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        receipt = self.receive('西渠', 'w', [3, 9])
        book = publish_day(self.db, DAY, [receipt.identity], 'pack-20', '丁账')
        ref = book.manifest.volumes[0].volume
        body = VolumeStorage(self.db).get_hot_bytes(ref.volume_id)
        self.assertIsNotNone(body)
        offset = single_pack(media, 'W', body)
        admit_pack(self.db, media, 'W', [(ref.volume_id, offset, ref.byte_length)])
        blocked = authorize_pull(self.db, media, 'W')
        self.assertEqual(blocked, PullBlocked((ref.volume_id,)))

    def test_mismatched_candidate_is_not_trusted(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        admit_pack(self.db, media, 'P', [tuple(row) for row in sample.layouts['P']])
        admit_pack(self.db, media, 'Q', [tuple(row) for row in sample.layouts['Q']])
        x_id = sample.volumes['X'].volume_id
        y_id = sample.volumes['Y'].volume_id
        _, q_offset, q_length = sample.layouts['Q'][0]
        _, z_offset, z_length = sample.layouts['Q'][1]
        self.assertEqual(q_length, z_length)
        z_bytes = media.read_slice('Q', z_offset, z_length)
        media.corrupt_slice('Q', q_offset, z_bytes)
        blocked = authorize_pull(self.db, media, 'P')
        self.assertEqual(blocked, PullBlocked((x_id, y_id)))
        admit_pack(self.db, media, 'S', [tuple(row) for row in sample.layouts['S']])
        blocked = authorize_pull(self.db, media, 'P')
        self.assertEqual(blocked, PullBlocked((x_id,)))
        self.assertFalse(PackStore(self.db).has_permit('P'))

    def test_bad_slice_breaks_new_admission(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        _, q_offset, q_length = sample.layouts['Q'][0]
        _, z_offset, z_length = sample.layouts['Q'][1]
        z_bytes = media.read_slice('Q', z_offset, z_length)
        new_offset = single_pack(media, 'R', z_bytes)
        with self.assertRaises(DataIntegrityError):
            admit_pack(self.db, media, 'R',
                       [(sample.volumes['X'].volume_id, new_offset, q_length)])
        self.assertFalse(PackStore(self.db).pack_exists('R'))

    def test_permit_reclaim_and_cross_pack_coverage(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        x_id = sample.volumes['X'].volume_id
        body = VolumeStorage(self.db).get_cold_bytes(x_id, '散冷柜')
        a_offset = single_pack(media, 'A', body)
        b_offset = single_pack(media, 'B', body)
        for pack, offset in (('A', a_offset), ('B', b_offset)):
            admit_pack(self.db, media, pack, [(x_id, offset, len(body))])
        self.assertEqual(authorize_pull(self.db, media, 'A'), PullPermit('A'))
        self.db.close()
        self.reopen()
        self.assertEqual(authorize_pull(self.db, media, 'A'), PullPermit('A'))
        self.assertEqual(authorize_pull(self.db, media, 'B'), PullBlocked((x_id,)))
        media.pull('A')
        result = read_cold_day(self.db, media, '甲账')
        self.assertIsInstance(result, ColdReadResult)
        self.assertEqual(result.evidence[0].pack_id, 'B')

    def test_permit_commit_failure_allows_retry(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        for pack in ('P', 'Q', 'S'):
            admit_pack(self.db, media, pack, [tuple(row) for row in sample.layouts[pack]])
        with fail_commit(self.db):
            with self.assertRaises(sqlite3.DatabaseError):
                authorize_pull(self.db, media, 'P')
        self.assertFalse(self.db.in_transaction)
        self.reopen()
        self.assertFalse(PackStore(self.db).has_permit('P'))
        self.assertEqual(authorize_pull(self.db, media, 'P'), PullPermit('P'))


class ColdReadTests(LakeCase):
    def test_cold_reads_after_pull(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        for pack in ('P', 'Q'):
            admit_pack(self.db, media, pack, [tuple(row) for row in sample.layouts[pack]])
        admit_pack(self.db, media, 'S', [tuple(row) for row in sample.layouts['S']])
        self.assertEqual(authorize_pull(self.db, media, 'P'), PullPermit('P'))
        self.db.close()
        media.pull('P')
        self.reopen()

        x_id = sample.volumes['X'].volume_id
        y_id = sample.volumes['Y'].volume_id
        jia = read_cold_day(self.db, media, '甲账')
        self.assertEqual([row.value for row in jia.details], [0, 7, None])
        self.assertEqual(jia.totals.liters, 7)
        self.assertEqual(len(jia.evidence), 1)
        self.assertEqual((jia.evidence[0].pack_id, jia.evidence[0].volume_id,
                          jia.evidence[0].offset, jia.evidence[0].byte_length,
                          jia.evidence[0].digest),
                         ('Q', x_id, sample.layouts['Q'][0][1],
                          sample.layouts['Q'][0][2], sample.volumes['X'].digest))

        yi = read_cold_day(self.db, media, '乙账')
        self.assertEqual([row.value for row in yi.details], [11, 25])
        self.assertEqual(yi.totals.liters, 11)
        self.assertEqual((yi.evidence[0].pack_id, yi.evidence[0].volume_id),
                         ('S', y_id))

        lian = read_cold_day(self.db, media, '联账')
        self.assertEqual([row.value for row in lian.details], [11, 25, 0, 7, None])
        self.assertEqual(lian.totals.liters, 18)
        self.assertEqual([(item.pack_id, item.volume_id) for item in lian.evidence],
                         [('S', y_id), ('Q', x_id)])
        self.assertEqual(lian.receipt.row_count, 5)

    def test_missing_only_in_fixed_order_without_partial_evidence(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        admit_pack(self.db, media, 'Q', [tuple(row) for row in sample.layouts['Q']])
        y_id = sample.volumes['Y'].volume_id
        x_id = sample.volumes['X'].volume_id
        unavailable = read_cold_day(self.db, media, '联账')
        self.assertIsInstance(unavailable, ReadUnavailable)
        self.assertEqual(unavailable.missing_volume_ids, (y_id,))
        self.assertEqual(unavailable.details, ())
        self.assertEqual(read_cold_day(self.db, media, '乙账').missing_volume_ids, (y_id,))
        self.assertEqual([row.value for row in read_cold_day(self.db, media, '甲账').details],
                         [0, 7, None])
        with self.assertRaises(KeyError):
            read_cold_day(self.db, media, 'unknown-account')

    def test_corrupt_slice_reports_missing_when_no_alternative(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        for pack in ('P', 'Q'):
            admit_pack(self.db, media, pack, [tuple(row) for row in sample.layouts[pack]])
        x_id = sample.volumes['X'].volume_id
        _, q_offset, q_length = sample.layouts['Q'][0]
        _, z_offset, z_length = sample.layouts['Q'][1]
        media.corrupt_slice('Q', q_offset, media.read_slice('Q', z_offset, z_length))
        media.pull('P')
        unavailable = read_cold_day(self.db, media, '甲账')
        self.assertEqual(unavailable.missing_volume_ids, (x_id,))
        self.assertEqual(unavailable.details, ())

    def test_present_permitted_pack_may_still_supply_evidence(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        for pack in ('P', 'Q', 'S'):
            admit_pack(self.db, media, pack, [tuple(row) for row in sample.layouts[pack]])
        self.assertEqual(authorize_pull(self.db, media, 'P'), PullPermit('P'))
        _, q_offset, q_length = sample.layouts['Q'][0]
        _, z_offset, z_length = sample.layouts['Q'][1]
        media.corrupt_slice('Q', q_offset, media.read_slice('Q', z_offset, z_length))
        self.db.close()
        self.reopen()
        result = read_cold_day(self.db, media, '甲账')
        if isinstance(result, ColdReadResult):
            self.assertEqual([row.value for row in result.details], [0, 7, None])
            self.assertEqual(result.evidence[0].pack_id, 'P')
        else:
            self.assertEqual(result.missing_volume_ids, (sample.volumes['X'].volume_id,))
            self.assertEqual(result.details, ())

    def test_empty_daybook(self):
        publish_day(self.db, DAY, [], 'daily-20', '空账')
        result = read_cold_day(self.db, make_pack_sample(self.db).media, '空账')
        self.assertIsInstance(result, ColdReadResult)
        self.assertEqual((result.details, result.evidence), ((), ()))
        self.assertEqual((result.totals.liters, result.totals.missing,
                          result.totals.out_of_range), (0, 0, 0))
