-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema för discovery i PostgreSQL. Idempotent: körs vid varje start.

CREATE SCHEMA IF NOT EXISTS discovery;

-- Svarscache: källornas API-svar en kort tid (se svarscache.py).
-- skapad/giltig_till är Unix-tid i sekunder, samma typ i båda backenderna.
CREATE TABLE IF NOT EXISTS discovery.svarscache (
    nyckel TEXT PRIMARY KEY,
    kalla TEXT NOT NULL,
    operation TEXT NOT NULL,
    svar TEXT NOT NULL,
    skapad DOUBLE PRECISION NOT NULL,
    giltig_till DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS svarscache_giltig_till_idx ON discovery.svarscache (giltig_till);

-- Arbetsbibliotek: publikationer användaren valt att spara (se bibliotek.py).
-- id är "doi:<doi>" när posten har DOI, annars "<kalla>:<kalla_id>".
-- fulltext_metod: "pdf", "pdf+ocr" (minst en sida maskinläst), "jats" m.fl.
CREATE TABLE IF NOT EXISTS discovery.bibliotek_poster (
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
    fulltext_hamtad DOUBLE PRECISION,
    fulltext_status TEXT NOT NULL,
    licens TEXT,
    sidor TEXT,
    tecken_totalt INTEGER NOT NULL DEFAULT 0,
    sparad DOUBLE PRECISION NOT NULL,
    uppdaterad DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery.bibliotek_projekt (
    projekt TEXT NOT NULL,
    post_id TEXT NOT NULL REFERENCES discovery.bibliotek_poster(id) ON DELETE CASCADE,
    tillagd DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (projekt, post_id)
);
CREATE INDEX IF NOT EXISTS bibliotek_projekt_post_idx ON discovery.bibliotek_projekt (post_id);

-- Fulltexten i numrerade stycken med teckenpositioner i posten fulltext,
-- så att varje citat har en adress. 'simple' i stället för en språkkonfig:
-- biblioteket blandar språk, och ett språks stemming fördärvar ett annats.
CREATE TABLE IF NOT EXISTS discovery.bibliotek_stycken (
    id BIGSERIAL PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES discovery.bibliotek_poster(id) ON DELETE CASCADE,
    nr INTEGER NOT NULL,
    text TEXT NOT NULL,
    tecken_start INTEGER NOT NULL,
    tecken_slut INTEGER NOT NULL,
    sida INTEGER,
    text_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    UNIQUE (post_id, nr)
);
CREATE INDEX IF NOT EXISTS bibliotek_stycken_fts_idx ON discovery.bibliotek_stycken USING GIN (text_tsv);
