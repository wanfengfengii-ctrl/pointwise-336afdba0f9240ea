import sqlite3


class PackStore:
    """Admission records and pull permits in the caller's transaction."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def initialize(self):
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS packs (
                pack_id TEXT PRIMARY KEY NOT NULL
            )
        ''')
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS pack_slices (
                pack_id TEXT NOT NULL REFERENCES packs(pack_id),
                ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
                offset INTEGER NOT NULL CHECK (offset >= 0),
                byte_length INTEGER NOT NULL CHECK (byte_length > 0),
                digest TEXT NOT NULL,
                PRIMARY KEY (pack_id, ordinal)
            )
        ''')
        self.db.execute('''
            CREATE INDEX IF NOT EXISTS pack_slices_volume ON pack_slices (volume_id)
        ''')
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS pull_permits (
                pack_id TEXT PRIMARY KEY NOT NULL REFERENCES packs(pack_id)
            )
        ''')

    def pack_exists(self, pack_id: str) -> bool:
        return self.db.execute('SELECT 1 FROM packs WHERE pack_id = ?',
                               (pack_id,)).fetchone() is not None

    def get_slices(self, pack_id: str) -> tuple[tuple[str, int, int, str], ...]:
        """Return the admitted manifest as (volume_id, offset, byte_length, digest)."""
        return tuple(self.db.execute('''
            SELECT volume_id, offset, byte_length, digest FROM pack_slices
            WHERE pack_id = ? ORDER BY ordinal
        ''', (pack_id,)))

    def insert_pack(self, pack_id: str, slices):
        self.db.execute('INSERT INTO packs (pack_id) VALUES (?)', (pack_id,))
        self.db.executemany('''
            INSERT INTO pack_slices (pack_id, ordinal, volume_id, offset, byte_length, digest)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', [(pack_id, ordinal, *item) for ordinal, item in enumerate(slices, start=1)])

    def has_permit(self, pack_id: str) -> bool:
        return self.db.execute('SELECT 1 FROM pull_permits WHERE pack_id = ?',
                               (pack_id,)).fetchone() is not None

    def issue_permit(self, pack_id: str):
        self.db.execute('INSERT OR IGNORE INTO pull_permits (pack_id) VALUES (?)', (pack_id,))

    def candidate_slices(self, volume_id: str, exclude_pack: str):
        """Admitted, unpermitted slices of a volume outside the pack being authorized."""
        return tuple(self.db.execute('''
            SELECT s.pack_id, s.offset, s.byte_length, s.digest
            FROM pack_slices AS s
            LEFT JOIN pull_permits AS p ON p.pack_id = s.pack_id
            WHERE s.volume_id = ? AND s.pack_id <> ? AND p.pack_id IS NULL
            ORDER BY s.pack_id, s.ordinal
        ''', (volume_id, exclude_pack)))

    def cold_candidates(self, volume_id: str):
        """Every admitted slice of a volume; present permitted packs may still be read."""
        return tuple(self.db.execute('''
            SELECT pack_id, offset, byte_length, digest FROM pack_slices
            WHERE volume_id = ? ORDER BY pack_id, ordinal
        ''', (volume_id,)))
