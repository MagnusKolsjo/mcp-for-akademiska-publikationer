-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Vektortabell för bibliotekets semantiska sökning (sqlite-vec). Körs bara
-- när DISCOVERY_BIBLIOTEK_SEMANTISK är på. En virtuell tabell följer inte
-- främmande nycklar; bibliotek.py tar bort vektorerna när ett stycke tas bort.
-- Del av bas-schemat v1.0 (låst); ändringar läggs som migreringar i db.py.

CREATE VIRTUAL TABLE IF NOT EXISTS bibliotek_embeddings USING vec0(
    stycke_id INTEGER PRIMARY KEY,
    vektor FLOAT[768] distance_metric=cosine
);
