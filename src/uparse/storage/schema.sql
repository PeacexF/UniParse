PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    name         TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    status       TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY,
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    url      TEXT NOT NULL,
    status   TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error    TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    final_url    TEXT,
    status       TEXT NOT NULL,
    http_status  INTEGER,
    title        TEXT,
    depth        INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    loaded_at    TEXT,
    elapsed_ms   INTEGER,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY,
    page_id      INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    record_index INTEGER NOT NULL,
    collection   TEXT,
    identity     TEXT,
    confidence   REAL,
    data_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    id              INTEGER PRIMARY KEY,
    record_id       INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    value_json      TEXT,
    type            TEXT,
    confidence      REAL,
    source          TEXT,
    selector        TEXT,
    provenance_json TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id  INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    page_id    INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    error_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    url        TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_job      ON sources(job_id, status);
CREATE INDEX IF NOT EXISTS idx_pages_source     ON pages(source_id, status);
CREATE INDEX IF NOT EXISTS idx_pages_hash       ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_records_page     ON records(page_id);
CREATE INDEX IF NOT EXISTS idx_records_identity ON records(identity);
CREATE INDEX IF NOT EXISTS idx_fields_record    ON fields(record_id);
CREATE INDEX IF NOT EXISTS idx_fields_name      ON fields(name);
CREATE INDEX IF NOT EXISTS idx_errors_job       ON errors(job_id, error_type);

CREATE VIEW IF NOT EXISTS v_records AS
SELECT r.id          AS record_id,
       r.collection  AS collection,
       r.confidence  AS confidence,
       p.url         AS page_url,
       s.url         AS source_url,
       r.data_json   AS data_json
FROM records r
JOIN pages   p ON p.id = r.page_id
JOIN sources s ON s.id = p.source_id;
