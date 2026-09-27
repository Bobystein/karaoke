-- Quien esta en la fiesta. Sin contrasena, solo un nombre.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,        -- uuid4, va en cookie
    name        TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache de videos ya descargados. La clave es el id de YouTube.
CREATE TABLE IF NOT EXISTS songs (
    video_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    channel      TEXT,
    duration_s   INTEGER,
    file_path    TEXT,                   -- NULL mientras no se ha descargado
    thumb_url    TEXT,
    downloaded_at TEXT,
    play_count   INTEGER NOT NULL DEFAULT 0
);

-- La cola. Un renglon por peticion, aunque la cancion se repita.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES songs(video_id),
    session_id    TEXT REFERENCES sessions(id),
    requested_by  TEXT NOT NULL,          -- copia del nombre, por si se borra la sesion
    state         TEXT NOT NULL           -- ver maquina de estados en SPEC.md
                  CHECK (state IN ('queued','downloading','ready','playing','played','failed','removed')),
    position      INTEGER NOT NULL,       -- orden en la cola, editable por el admin
    error         TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    played_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(state, position);

-- Invariante: nunca dos canciones sonando a la vez. La base lo hace cumplir
-- ademas del codigo.
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_one_playing ON queue(state) WHERE state = 'playing';
