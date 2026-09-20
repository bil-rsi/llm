-- 0004: constant candidate pool so the planner can cost the HNSW scan.
-- Found with auto_explain: inside the (non-inlined) function, `LIMIT k * 4` is a parameter in the cached generic plan,
-- so the planner could not assume a small limit and chose "bitmap scan on the primary key + sort" (3.7 ms at 1000
-- memories) instead of the HNSW index scan (~0.3 ms). A constant pool of 64 nearest candidates restores the index scan;
-- the outer LIMIT k (k <= 20 in practice) picks the final rows after the user/status/expiry filter.
CREATE OR REPLACE FUNCTION app.nearest_memories(q vector, p_model text, p_user uuid, k integer)
RETURNS TABLE (id uuid, dist double precision)
LANGUAGE sql STABLE PARALLEL SAFE
SET enable_seqscan = off
SET enable_bitmapscan = off
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
