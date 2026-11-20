import sqlite3

from lake import ConflictError, transaction
from lake.custody.volumes import matches_volume
from lake.reading.daybook import read_day
from tests.pack_fixture import fail_commit, make_pack_sample
from tests.support import LakeCase


class PackFixtureTests(LakeCase):
    def test_compose_and_pull_physical_media(self):
        sample = make_pack_sample(self.db)
        media = sample.media
        refs = {ref.volume_id: ref for ref in sample.volumes.values()}
        for key, layout in sample.layouts.items():
            for volume_id, offset, byte_length in layout:
                self.assertTrue(matches_volume(refs[volume_id], media.read_slice(key, offset, byte_length)))

        x = sample.volumes["X"]
        y = sample.volumes["Y"]
        self.assertEqual(read_day(self.db, "联账").missing_volume_ids, (y.volume_id, x.volume_id))
        self.db.close()
        media.pull("P")
        self.assertIsNone(media.read_slice("P", *sample.layouts["P"][0][1:]))
        with self.assertRaises(ConflictError):
            media.mount("P", b"different medium")
        self.reopen()
        self.assertTrue(matches_volume(x, media.read_slice("Q", *sample.layouts["Q"][0][1:])))
        self.assertTrue(matches_volume(y, media.read_slice("S", *sample.layouts["S"][0][1:])))

        _, offset, length = sample.layouts["Q"][0]
        wrong = media.read_slice("Q", *sample.layouts["Q"][1][1:])
        self.assertEqual(len(wrong), length)
        media.corrupt_slice("Q", offset, wrong)
        self.assertFalse(matches_volume(x, media.read_slice("Q", offset, length)))

    def test_sqlite_commit_veto_allows_rollback(self):
        self.db.execute("CREATE TABLE fixture_commit_probe (id INTEGER PRIMARY KEY)")
        with fail_commit(self.db):
            with self.assertRaises(sqlite3.DatabaseError):
                with transaction(self.db, write=True):
                    self.db.execute("INSERT INTO fixture_commit_probe VALUES (1)")
        self.assertFalse(self.db.in_transaction)
        self.reopen()
        self.assertEqual(self.db.execute("SELECT id FROM fixture_commit_probe").fetchall(), [])
        with transaction(self.db, write=True):
            self.db.execute("INSERT INTO fixture_commit_probe VALUES (1)")
        self.assertEqual(self.db.execute("SELECT id FROM fixture_commit_probe").fetchall(), [(1,)])
