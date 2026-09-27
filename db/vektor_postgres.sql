-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Vektortabell för bibliotekets semantiska sökning (pgvector). Körs bara
-- när DISCOVERY_BIBLIOTEK_SEMANTISK är på. 768 dimensioner, samma som
-- projektets embeddingmodeller. Inget ANN-index: ett arbetsbibliotek har
-- tusentals, inte miljoner, stycken, och en full genomsökning är både
-- snabb nog och exakt.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS discovery.bibliotek_embeddings (
    stycke_id BIGINT PRIMARY KEY REFERENCES discovery.bibliotek_stycken(id) ON DELETE CASCADE,
    vektor vector(768) NOT NULL
);
