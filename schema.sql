-- Who's at the party. No password, just a name.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,        -- uuid4, stored in a cookie
    name        TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache of downloaded videos. The key is the YouTube id.
CREATE TABLE IF NOT EXISTS songs (
    video_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    channel      TEXT,
    duration_s   INTEGER,
    file_path    TEXT,                   -- NULL until downloaded
    thumb_url    TEXT,
    downloaded_at TEXT,
    play_count   INTEGER NOT NULL DEFAULT 0
);

-- The queue. One row per request, even if the song repeats.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES songs(video_id),
    session_id    TEXT REFERENCES sessions(id),
    requested_by  TEXT NOT NULL,          -- copy of the name, in case the session is deleted
    state         TEXT NOT NULL           -- see the state machine in SPEC.md
                  CHECK (state IN ('queued','downloading','ready','playing','played','failed','removed')),
    position      INTEGER NOT NULL,       -- order in the queue, editable by the admin
    error         TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    played_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(state, position);

-- Invariant: never two songs playing at once. The database enforces it
-- in addition to the code.
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_one_playing ON queue(state) WHERE state = 'playing';
