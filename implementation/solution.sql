DROP SCHEMA IF EXISTS rank_release CASCADE;
CREATE SCHEMA rank_release;

CREATE TABLE rank_release.content_catalog(
  content_id text PRIMARY KEY,
  title text NOT NULL,
  active boolean NOT NULL
);

CREATE TABLE rank_release.content_event(
  event_id text PRIMARY KEY,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL,
  region text NOT NULL,
  content_id text NOT NULL REFERENCES rank_release.content_catalog(content_id),
  event_type text NOT NULL CHECK(event_type IN('impression','click')),
  dwell_ms integer NOT NULL CHECK(dwell_ms>=0)
);

CREATE TABLE rank_release.release_batch(
  batch_id text PRIMARY KEY,
  stage text NOT NULL UNIQUE,
  cutoff_received_at timestamptz NOT NULL,
  source_event_count integer NOT NULL CHECK(source_event_count>=0),
  published_row_count integer NOT NULL CHECK(published_row_count>=0),
  status text NOT NULL CHECK(status IN('READY','FAILED')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE rank_release.published_rank(
  batch_id text NOT NULL REFERENCES rank_release.release_batch(batch_id),
  hour_start timestamptz NOT NULL,
  region text NOT NULL,
  content_id text NOT NULL,
  impression_count bigint NOT NULL,
  click_count bigint NOT NULL,
  total_dwell_ms bigint NOT NULL,
  ctr_pct numeric(12,4) NOT NULL,
  region_rank bigint NOT NULL,
  PRIMARY KEY(batch_id,hour_start,region,content_id)
);

CREATE TABLE rank_release.active_release(
  singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
  batch_id text NOT NULL REFERENCES rank_release.release_batch(batch_id)
);

CREATE OR REPLACE FUNCTION rank_release.publish_batch(
  p_stage text,
  p_batch_id text,
  p_cutoff timestamptz,
  p_lock_wait_ms integer
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
  v_event_count integer;
  v_rank_count integer;
BEGIN
  IF p_lock_wait_ms<=0 THEN RAISE EXCEPTION 'lock_wait_ms must be positive'; END IF;
  PERFORM set_config('lock_timeout',p_lock_wait_ms::text||'ms',true);
  PERFORM pg_advisory_xact_lock(hashtext('rank_release.publish'));
  IF EXISTS(SELECT 1 FROM rank_release.release_batch WHERE batch_id=p_batch_id OR stage=p_stage) THEN
    RAISE EXCEPTION 'release batch already exists';
  END IF;
  SELECT count(*) INTO v_event_count FROM rank_release.content_event WHERE received_at<=p_cutoff;
  INSERT INTO rank_release.release_batch(batch_id,stage,cutoff_received_at,source_event_count,published_row_count,status)
  VALUES(p_batch_id,p_stage,p_cutoff,v_event_count,0,'READY');
  WITH aggregated AS(
    SELECT date_trunc('hour',e.occurred_at) hour_start,e.region,e.content_id,
      count(*) FILTER(WHERE e.event_type='impression') impression_count,
      count(*) FILTER(WHERE e.event_type='click') click_count,
      coalesce(sum(e.dwell_ms) FILTER(WHERE e.event_type='click'),0) total_dwell_ms
    FROM rank_release.content_event e
    JOIN rank_release.content_catalog c USING(content_id)
    WHERE c.active AND e.received_at<=p_cutoff
    GROUP BY 1,2,3
  ), ranked AS(
    SELECT *,round(100.0*click_count/nullif(impression_count,0),4) ctr_pct,
      rank() OVER(PARTITION BY hour_start,region ORDER BY click_count DESC,round(100.0*click_count/nullif(impression_count,0),4) DESC,total_dwell_ms DESC,content_id ASC) region_rank
    FROM aggregated
  )
  INSERT INTO rank_release.published_rank
  SELECT p_batch_id,hour_start,region,content_id,impression_count,click_count,total_dwell_ms,coalesce(ctr_pct,0),region_rank FROM ranked;
  GET DIAGNOSTICS v_rank_count=ROW_COUNT;
  UPDATE rank_release.release_batch SET published_row_count=v_rank_count WHERE batch_id=p_batch_id;
  INSERT INTO rank_release.active_release(singleton,batch_id) VALUES(true,p_batch_id)
  ON CONFLICT(singleton) DO UPDATE SET batch_id=excluded.batch_id;
END
$$;
