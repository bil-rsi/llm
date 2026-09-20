-- 0002: deterministic HNSW use for memory nearest-neighbour search.
-- For small/medium tables the planner sometimes prefers "seq scan + sort" over the HNSW index (measured 3.5-4.8 ms vs
-- 0.6 ms at 1000 memories). The function pins enable_seqscan=off for this one query only (function-level SET), so the
-- ORDER BY embedding <=> q LIMIT k is always answered by the index. Everything else keeps the normal planner.
CREATE OR REPLACE FUNCTION app.nearest_memories(q vector, p_model text, p_user uuid, k integer)
RETURNS TABLE (id uuid, dist double precision)
LANGUAGE sql STABLE PARALLEL SAFE
SET enable_seqscan = off
AS $$
  SELECT m.id, (e.embedding <=> q)::double precision AS dist
  FROM app.memory_embeddings e JOIN app.long_term_memories m ON m.id = e.memory_id
  WHERE e.model = p_model AND m.user_id = p_user AND m.status = 'active' AND (m.expires_at IS NULL OR m.expires_at > now())
  ORDER BY e.embedding <=> q
  LIMIT k
$$;

GRANT EXECUTE ON FUNCTION app.nearest_memories(vector, text, uuid, integer) TO aimem_app;
