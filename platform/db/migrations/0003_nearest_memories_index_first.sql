-- 0003: nearest-neighbour search driven by the HNSW index first, filters second.
-- With the user/status join in the same ORDER BY ... LIMIT, the planner walked every row of the user's memories via
-- ltm_user_status_idx and sorted by distance (3.5 ms at 1000 memories, detoasting each 4 KB vector). Searching the
-- HNSW index alone takes ~0.3 ms. Over-fetch 4*k candidates, then apply user/status/expiry: vectors are deleted as soon
-- as a memory leaves active/candidate, so the filter rarely drops candidates (and this is a single-user system).
CREATE OR REPLACE FUNCTION app.nearest_memories(q vector, p_model text, p_user uuid, k integer)
RETURNS TABLE (id uuid, dist double precision)
LANGUAGE sql STABLE PARALLEL SAFE
SET enable_seqscan = off
AS $$
  WITH nn AS MATERIALIZED (
    SELECT e.memory_id, (e.embedding <=> q)::double precision AS dist
    FROM app.memory_embeddings e
    WHERE e.model = p_model
    ORDER BY e.embedding <=> q
    LIMIT k * 4
  )
  SELECT m.id, nn.dist
  FROM nn JOIN app.long_term_memories m ON m.id = nn.memory_id
  WHERE m.user_id = p_user AND m.status = 'active' AND (m.expires_at IS NULL OR m.expires_at > now())
  ORDER BY nn.dist
  LIMIT k
$$;
