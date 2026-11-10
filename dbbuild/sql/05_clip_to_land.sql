-- Recorta los polígonos de INEGI al borde terrestre (aoi_extent, el mismo AOI
-- con que se recortan los rasters de WorldClim y DEM). El shape nacional trae
-- el mar como CUERPO DE AGUA (polígonos de cientos de miles de km²) y las
-- mallas solo contemplan áreas terrestres.
--
-- __RAW_TABLE__: shape original cargado por ogr2ogr (EPSG:4326).
-- __AOI_TABLE__: AOI terrestre cargado por ogr2ogr.
-- __USV_TABLE__: tabla final recortada.

-- AOI subdividido para que el índice espacial rinda (un solo multipolígono de
-- 817 partes haría cada ST_Intersects contra toda la costa de América).
DROP TABLE IF EXISTS usv_aoi_parts;
CREATE TABLE usv_aoi_parts AS
SELECT ST_Subdivide(ST_MakeValid(a.geom), 256) AS geom
FROM __AOI_TABLE__ a
WHERE a.geom && (SELECT ST_Extent(the_geom) FROM __RAW_TABLE__);
CREATE INDEX ON usv_aoi_parts USING GIST (geom);
ANALYZE usv_aoi_parts;

DROP TABLE IF EXISTS __USV_TABLE__;
CREATE TABLE __USV_TABLE__ AS
WITH pieces AS (
  SELECT
    p.id,
    -- Atajo: si la pieza del AOI cubre el polígono completo no hay que intersectar.
    CASE WHEN ST_CoveredBy(p.the_geom, a.geom) THEN p.the_geom
         ELSE ST_CollectionExtract(ST_Intersection(p.the_geom, a.geom), 3)
    END AS geom
  FROM __RAW_TABLE__ p
  JOIN usv_aoi_parts a
    ON p.the_geom && a.geom
   AND ST_Intersects(p.the_geom, a.geom)
),
clipped AS (
  SELECT id, ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_UnaryUnion(ST_Collect(geom))), 3)) AS the_geom
  FROM pieces
  WHERE NOT ST_IsEmpty(geom)
  GROUP BY id
)
SELECT r.id, r.clave, r.descripcio, c.the_geom::geometry(MultiPolygon, 4326) AS the_geom
FROM clipped c
JOIN __RAW_TABLE__ r ON r.id = c.id
WHERE NOT ST_IsEmpty(c.the_geom);

ALTER TABLE __USV_TABLE__ ADD PRIMARY KEY (id);
