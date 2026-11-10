CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS usv_data_source_info (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    description TEXT,
    source_url TEXT,
    download_url TEXT,
    dict_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Una fila por serie/edición cargada (ej. Serie VII). La tabla espacial de cada
-- serie es usv_{series} (ej. usv_serie_vii).
CREATE TABLE IF NOT EXISTS usv_source_vars (
    id SERIAL PRIMARY KEY,
    data_source_id INTEGER NOT NULL REFERENCES usv_data_source_info(id) ON DELETE RESTRICT,
    source_code TEXT NOT NULL,
    fuente TEXT NOT NULL,
    area TEXT NOT NULL,
    series TEXT NOT NULL,
    descripcion TEXT NOT NULL,
    level_size INTEGER,
    available_grids INTEGER[] NOT NULL DEFAULT ARRAY[]::INTEGER[],
    filter_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_name TEXT NOT NULL,
    variable_name TEXT NOT NULL,
    scale TEXT NOT NULL,
    edition TEXT,
    categories_count INTEGER NOT NULL,
    shape_file TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_code, area, series)
);

-- Clases de uso de suelo y vegetación (equivalente a dem_bins). clave es la
-- CLAVE de INEGI (ej. BQ, TA, VSa/SBC) y es estable entre corridas, por eso el
-- build conserva el id de cada (source_var_id, clave).
CREATE TABLE IF NOT EXISTS usv_categories (
    id BIGSERIAL PRIMARY KEY,
    source_var_id INTEGER NOT NULL REFERENCES usv_source_vars(id) ON DELETE CASCADE,
    cat_index INTEGER NOT NULL,
    clave TEXT NOT NULL,
    label TEXT NOT NULL,
    grupo TEXT NOT NULL,
    layer TEXT NOT NULL,
    n_polygons INTEGER NOT NULL DEFAULT 0,
    area_km2 DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_var_id, clave)
);

-- Búsqueda de clases por texto sin acentos ni mayúsculas ("galeria" encuentra
-- "BOSQUE DE GALERÍA"); evita depender de la extensión unaccent.
CREATE OR REPLACE FUNCTION unaccent_label(text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT lower(translate($1, 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
$$;

-- Nota: NO reiniciar secuencia aquí. Este script corre en cada ejecución del ETL.
-- El build fija el inicio en 500000 solo si la tabla está vacía.

CREATE INDEX IF NOT EXISTS idx_usv_categories_source_var_id ON usv_categories (source_var_id);
CREATE INDEX IF NOT EXISTS idx_usv_source_vars_data_source_id ON usv_source_vars (data_source_id);
