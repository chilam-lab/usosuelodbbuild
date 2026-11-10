DO $$
DECLARE
  t record;
  total_targets int;
  i int := 0;
  total_ok int := 0;
  v_sql text;
  has_presence boolean;
  has_region_col boolean;
  has_border_col boolean;
  t0 timestamptz;
  target_table text := '__USV_TABLE__';
  target_source_var_id integer := __SOURCE_VAR_ID__;
  result_grids int4[];
BEGIN
  t0 := clock_timestamp();

  IF to_regclass(format('public.%I', target_table)) IS NULL THEN
    RAISE EXCEPTION 'USV table % not found', target_table;
  END IF;

  CREATE TEMP TABLE tmp_usv_mesh_presence (
    table_view_name text,
    region_id int4,
    has_presence boolean,
    PRIMARY KEY (table_view_name, region_id)
  ) ON COMMIT DROP;

  SELECT count(*)
  INTO total_targets
  FROM (
    SELECT DISTINCT table_view_name, region_id
    FROM usv_mesh_fdw.cat_grid
    WHERE table_view_name IS NOT NULL
      AND region_id IS NOT NULL
  ) q;

  RAISE NOTICE '[usv_available_grids] Inicio. combinaciones (vista,region)=%', total_targets;

  FOR t IN
    SELECT DISTINCT table_view_name, region_id
    FROM usv_mesh_fdw.cat_grid
    WHERE table_view_name IS NOT NULL
      AND region_id IS NOT NULL
    ORDER BY table_view_name, region_id
  LOOP
    i := i + 1;

    IF to_regclass(format('usv_mesh_fdw.%I', t.table_view_name)) IS NULL THEN
      INSERT INTO tmp_usv_mesh_presence VALUES (t.table_view_name, t.region_id, false)
      ON CONFLICT DO NOTHING;
      CONTINUE;
    END IF;

    SELECT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = 'usv_mesh_fdw'
        AND table_name = t.table_view_name
        AND column_name = 'region_id'
    ) INTO has_region_col;

    SELECT EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_schema = 'usv_mesh_fdw'
        AND table_name = t.table_view_name
        AND column_name = 'border'
    ) INTO has_border_col;

    IF NOT has_region_col OR NOT has_border_col THEN
      INSERT INTO tmp_usv_mesh_presence VALUES (t.table_view_name, t.region_id, false)
      ON CONFLICT DO NOTHING;
      CONTINUE;
    END IF;

    v_sql := format($f$
      SELECT EXISTS (
        SELECT 1
        FROM public.%I d
        JOIN usv_mesh_fdw.%I g
          ON g.region_id = %s
         AND d.the_geom IS NOT NULL
         AND d.the_geom && g.border
         AND ST_Intersects(d.the_geom, g.border)
        LIMIT 1
      )
    $f$, target_table, t.table_view_name, t.region_id);

    BEGIN
      EXECUTE v_sql INTO has_presence;
    EXCEPTION WHEN OTHERS THEN
      has_presence := false;
    END;

    IF has_presence THEN
      total_ok := total_ok + 1;
    END IF;

    INSERT INTO tmp_usv_mesh_presence (table_view_name, region_id, has_presence)
    VALUES (t.table_view_name, t.region_id, has_presence)
    ON CONFLICT (table_view_name, region_id)
    DO UPDATE SET has_presence = EXCLUDED.has_presence;
  END LOOP;

  SELECT COALESCE(array_agg(cg.grid_id ORDER BY cg.grid_id), '{}'::int4[])
  INTO result_grids
  FROM usv_mesh_fdw.cat_grid cg
  JOIN tmp_usv_mesh_presence p
    ON p.table_view_name = cg.table_view_name
   AND p.region_id = cg.region_id
  WHERE p.has_presence;

  UPDATE usv_source_vars
  SET available_grids = result_grids,
      updated_at = NOW()
  WHERE id = target_source_var_id;

  RAISE NOTICE '[usv_available_grids] Fin. combinaciones_con_presencia=% de %', total_ok, total_targets;
  RAISE NOTICE '[usv_available_grids] Fin. tiempo=%', (clock_timestamp() - t0);
END$$;
