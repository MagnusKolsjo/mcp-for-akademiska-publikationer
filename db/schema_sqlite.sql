-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema för discovery i SQLite. Idempotent: körs vid varje start.
-- Se schema_postgres.sql för kommentarer om tabellernas innehåll.

CREATE TABLE IF NOT EXISTS svarscache (
    nyckel TEXT PRIMARY KEY,
    kalla TEXT NOT NULL,
    operation TEXT NOT NULL,
    svar TEXT NOT NULL,
    skapad DOUBLE PRECISION NOT NULL,
    giltig_till DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS svarscache_giltig_till_idx ON svarscache (giltig_till);

CREATE TABLE IF NOT EXISTS bibliotek_poster (
    id TEXT PRIMARY KEY,
    kalla TEXT NOT NULL,
    kalla_id TEXT,
    doi TEXT,
    titel TEXT,
    forfattare TEXT,
    ar INTEGER,
    typ TEXT,
    url TEXT,
    sprak TEXT,
    sammanfattning TEXT,
    referens TEXT,
    fulltext TEXT,
    fulltext_url TEXT,
    fulltext_metod TEXT,
    fulltext_status TEXT NOT NULL,
    licens TEXT,
    sidor TEXT,
    tecken_totalt INTEGER NOT NULL DEFAULT 0,
    sparad DOUBLE PRECISION NOT NULL,
    uppdaterad DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS bibliotek_projekt (
    projekt TEXT NOT NULL,
    post_id TEXT NOT NULL REFERENCES bibliotek_poster(id) ON DELETE CASCADE,
    tillagd DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (projekt, post_id)
);
CREATE INDEX IF NOT EXISTS bibliotek_projekt_post_idx ON bibliotek_projekt (post_id);

CREATE TABLE IF NOT EXISTS bibliotek_stycken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES bibliotek_poster(id) ON DELETE CASCADE,
    nr INTEGER NOT NULL,
    text TEXT NOT NULL,
    tecken_start INTEGER NOT NULL,
    tecken_slut INTEGER NOT NULL,
    sida INTEGER,
    UNIQUE (post_id, nr)
);

-- FTS5 med extern innehållstabell; triggrarna håller indexet i takt med
-- bibliotek_stycken. remove_diacritics 2 gör att "sakerhet" hittar
-- "säkerhet" utan att svenska ord slås ihop med varandra i onödan.
CREATE VIRTUAL TABLE IF NOT EXISTS bibliotek_stycken_fts USING fts5(
    text, content='bibliotek_stycken', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS bibliotek_stycken_ai AFTER INSERT ON bibliotek_stycken BEGIN
    INSERT INTO bibliotek_stycken_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS bibliotek_stycken_ad AFTER DELETE ON bibliotek_stycken BEGIN
    INSERT INTO bibliotek_stycken_fts(bibliotek_stycken_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
