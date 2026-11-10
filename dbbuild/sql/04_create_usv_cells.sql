-- Celdas precalculadas por clase de uso de suelo (usv_categories.id) y malla
-- (cat_grid.grid_id). La llena precompute_usv_cells.py; el middleware
-- (get-data) la lee antes de calcular en vivo. Mismo resultado que el cálculo
-- en vivo: celdas de la región del grid_id que intersectan los polígonos de la clase.
CREATE TABLE IF NOT EXISTS usv_cells (
  cid        integer NOT NULL,
  grid_id    integer NOT NULL,
  cells      integer[] NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (cid, grid_id)
);
