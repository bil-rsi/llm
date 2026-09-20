-- 0005: make the HNSW index the only viable plan for the nearest-neighbour step.
-- auto_explain after 0004 still showed "index scan on the primary key + top-N sort": pgvector's cost estimate for the
-- HNSW scan is pessimistic on small tables (≈470 for a 64-row pool vs ≈190 for pkey scan + sort). With enable_sort off
-- (scoped to this function only) any plan that must sort by distance is penalised, and the index — which returns rows
-- already ordered by distance — wins. The outer ORDER BY over <= 64 candidates is equally penalised in every plan, so
-- it does not change the choice.
CREATE OR REPLACE FUNCTION app.nearest_memories(q vector, p_model text, p_user uuid, k integer)
RETURNS TABLE (id uuid, dist double precision)
LANGUAGE sql STABLE PARALLEL SAFE
SET enable_seqscan = off
SET enable_bitmapscan = off
SET enable_sort = off
AS $$
  WITH nn AS MATERIALIZED (
    SELECT e.memory_id, (e.embedding <=> q)::double precision AS dist
    FROM app.memory_embeddings e
    WHERE e.model = p_model
    ORDER BY e.embedding <=> q
    LIMIT 64
  )
  SELECT m.id, nn.dist
  FROM nn JOIN app.long_term_memories m ON m.id = nn.memory_id
  WHERE m.user_id = p_user AND m.status = 'active' AND (m.expires_at IS NULL OR m.expires_at > now())
  ORDER BY nn.dist
  LIMIT least(k, 64)
$$;
