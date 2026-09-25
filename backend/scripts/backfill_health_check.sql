-- Backfill health check for the TradingMaster PostgreSQL database.
-- Read-only: every statement is a SELECT. Run it on the server with:
--
--   cd /opt/tradingmaster
--   docker compose -f docker-compose.prod.yml --env-file .env exec -T postgres \
--     sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backfill_health_check.sql
--
-- Times are shown in IST.

SET TIME ZONE 'Asia/Kolkata';
SET default_transaction_read_only = on;
SET statement_timeout = '120s';
\pset footer off

\echo
\echo '== 1. Backfill jobs by status, last 30 days'
SELECT source, timeframe, status, count(*) AS jobs, coalesce(sum(inserted_count), 0) AS bars_saved,
       to_char(max(created_at), 'DD-Mon HH24:MI') AS latest
FROM bf_backfill_jobs
WHERE created_at > now() - interval '30 days'
GROUP BY source, timeframe, status
ORDER BY source, timeframe, status;

\echo
\echo '== 2. Jobs stuck in running/pending for over 30 minutes (should be none)'
SELECT s.symbol, j.source, j.timeframe, j.status, to_char(j.created_at, 'DD-Mon HH24:MI') AS created,
       to_char(j.started_at, 'DD-Mon HH24:MI') AS started
FROM bf_backfill_jobs j JOIN bf_symbols s ON s.id = j.symbol_id
WHERE j.status IN ('running', 'pending') AND j.created_at < now() - interval '30 minutes'
ORDER BY j.created_at
LIMIT 50;

\echo
\echo '== 3. Failure reasons, most common first'
SELECT left(error_message, 110) AS reason, count(*) AS jobs, to_char(max(completed_at), 'DD-Mon HH24:MI') AS last_seen
FROM bf_backfill_jobs
WHERE status = 'failed'
GROUP BY left(error_message, 110)
ORDER BY count(*) DESC
LIMIT 20;

\echo
\echo '== 4. Completed jobs that downloaded nothing, most recent first'
SELECT s.symbol, j.source, j.timeframe, j.start_date, j.end_date, to_char(j.completed_at, 'DD-Mon HH24:MI') AS completed
FROM bf_backfill_jobs j JOIN bf_symbols s ON s.id = j.symbol_id
WHERE j.status = 'completed' AND j.downloaded_count = 0
ORDER BY j.completed_at DESC
LIMIT 20;

\echo
\echo '== 5. Stored backfill bars by source and timeframe'
SELECT s.source, b.timeframe, count(DISTINCT b.symbol_id) AS symbols, count(*) AS bars,
       to_char(min(b.ts), 'DD-Mon-YYYY') AS first_bar, to_char(max(b.ts), 'DD-Mon-YYYY HH24:MI') AS last_bar
FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
GROUP BY s.source, b.timeframe
ORDER BY s.source, b.timeframe;

\echo
\echo '== 6. Tracked symbols with no bars at all'
SELECT source, count(*) AS symbols, string_agg(symbol, ', ' ORDER BY symbol) FILTER (WHERE n <= 15) AS first_15
FROM (
  SELECT s.source, s.symbol, row_number() OVER (PARTITION BY s.source ORDER BY s.symbol) AS n
  FROM bf_symbols s
  WHERE NOT EXISTS (SELECT 1 FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id)
) empty
GROUP BY source;

\echo
\echo '== 7. Backfilled but not yet copied to Charts/strategies (catalog sync backlog)'
WITH latest AS (
  SELECT symbol_id, max(completed_at) AS completed_at FROM bf_backfill_jobs WHERE status = 'completed' GROUP BY symbol_id
)
SELECT count(*) AS symbols_waiting, to_char(min(l.completed_at), 'DD-Mon HH24:MI') AS oldest_waiting_since
FROM latest l JOIN bf_symbols s ON s.id = l.symbol_id
WHERE s.last_synced_at IS NULL OR l.completed_at > s.last_synced_at;

\echo
\echo '== 8. Symbols with fewer candles in the main table than were backfilled (copy gaps)'
WITH bf AS (
  SELECT s.source, s.symbol, b.timeframe, count(*) AS backfilled
  FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
  GROUP BY s.source, s.symbol, b.timeframe
), main AS (
  SELECT i.exchange, i.symbol, c.timeframe, count(*) AS in_main
  FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  GROUP BY i.exchange, i.symbol, c.timeframe
)
SELECT bf.source, bf.symbol, bf.timeframe, bf.backfilled, coalesce(main.in_main, 0) AS in_main
FROM bf
LEFT JOIN main ON main.symbol = bf.symbol AND main.timeframe = bf.timeframe
  AND main.exchange = CASE bf.source WHEN 'zerodha' THEN 'NSE' WHEN 'zerodha_nfo' THEN 'NFO' ELSE 'DELTA' END
WHERE coalesce(main.in_main, 0) < bf.backfilled
ORDER BY bf.backfilled - coalesce(main.in_main, 0) DESC
LIMIT 30;

\echo
\echo '== 9. Intraday history (5m-60m), last 14 days: symbols with the fewest trading days of data'
SELECT s.source, s.symbol, b.timeframe,
       count(DISTINCT (b.ts AT TIME ZONE 'Asia/Kolkata')::date) AS days_with_data,
       to_char(max(b.ts), 'DD-Mon HH24:MI') AS last_bar
FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
WHERE b.timeframe IN ('5m', '15m', '30m', '60m') AND b.ts > now() - interval '14 days'
GROUP BY s.source, s.symbol, b.timeframe
ORDER BY days_with_data, s.symbol
LIMIT 25;

\echo
\echo '== 10. Database and table sizes'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows_estimate
FROM pg_stat_user_tables
WHERE relname IN ('bf_ohlcv_bars', 'bf_backfill_jobs', 'bf_symbols', 'ohlcv_candles', 'instruments')
ORDER BY pg_total_relation_size(relid) DESC;
