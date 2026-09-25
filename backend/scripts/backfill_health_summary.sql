-- Backfill health summary: counts and statuses only -- no symbol names,
-- no error text -- so it is safe to print in a public CI log (see
-- .github/workflows/backfill-health-check.yml). backfill_health_check.sql
-- next to it has the per-symbol detail. Read-only, like that one.

SET TIME ZONE 'Asia/Kolkata';
SET default_transaction_read_only = on;
SET statement_timeout = '120s';
\pset footer off

\echo
\echo '== A. Backfill jobs by status, all time'
SELECT source, status, count(*) AS jobs, coalesce(sum(inserted_count), 0) AS bars_saved,
       to_char(max(created_at), 'DD-Mon-YYYY HH24:MI') AS latest
FROM bf_backfill_jobs
GROUP BY source, status
ORDER BY source, status;

\echo
\echo '== B. Backfill jobs by timeframe, last 30 days'
SELECT source, timeframe, status, count(*) AS jobs, coalesce(sum(inserted_count), 0) AS bars_saved,
       to_char(max(created_at), 'DD-Mon HH24:MI') AS latest
FROM bf_backfill_jobs
WHERE created_at > now() - interval '30 days'
GROUP BY source, timeframe, status
ORDER BY source, timeframe, status;

\echo
\echo '== C. Jobs running or queued right now'
SELECT status, count(*) AS jobs,
       count(*) FILTER (WHERE created_at < now() - interval '30 minutes') AS older_than_30_min,
       to_char(min(created_at), 'DD-Mon HH24:MI') AS oldest
FROM bf_backfill_jobs
WHERE status IN ('running', 'pending')
GROUP BY status;

\echo
\echo '== D. Failed jobs by cause'
SELECT CASE
         WHEN error_message IS NULL THEN 'no message'
         WHEN error_message LIKE 'Interrupted by a server restart%' THEN 'interrupted by a server restart'
         WHEN error_message LIKE 'Symbol no longer exists%' THEN 'symbol deleted'
         WHEN error_message ILIKE '%value too long%' OR error_message ILIKE '%StringDataRightTruncation%' THEN 'text too long for a column'
         WHEN error_message ILIKE '%duplicate key%' OR error_message ILIKE '%UniqueViolation%' OR error_message ILIKE '%IntegrityError%' THEN 'duplicate bar (overlapping backfills)'
         WHEN error_message ILIKE '%exceeds max limit%' OR error_message ILIKE '%interval exceeds%' THEN 'date range longer than Kite allows'
         WHEN error_message ILIKE '%too many requests%' OR error_message ILIKE '% 429%' THEN 'rate limited'
         WHEN error_message ILIKE '%token%' OR error_message ILIKE '%api_key%' OR error_message ILIKE '%not connected%'
              OR error_message ILIKE '%login%' OR error_message ILIKE '% 403%' THEN 'Kite login/session'
         WHEN error_message ILIKE '%timeout%' OR error_message ILIKE '%timed out%' OR error_message ILIKE '%ReadError%'
              OR error_message ILIKE '%ConnectError%' OR error_message ILIKE '%connection%' THEN 'network'
         WHEN error_message ILIKE '% 5__ %' OR error_message ILIKE '%error 5__%' THEN 'source server error (5xx)'
         ELSE 'other'
       END AS cause,
       count(*) AS jobs,
       count(*) FILTER (WHERE completed_at > now() - interval '7 days') AS last_7_days,
       to_char(max(completed_at), 'DD-Mon-YYYY HH24:MI') AS last_seen
FROM bf_backfill_jobs
WHERE status = 'failed'
GROUP BY 1
ORDER BY count(*) DESC;

\echo
\echo '== E. Jobs failed by a server restart: how long had they been running?'
\echo '   (a job left "running" by an unhandled error was only closed at the next restart)'
SELECT CASE
         WHEN started_at IS NULL THEN 'still queued'
         WHEN completed_at - started_at > interval '30 minutes' THEN 'over 30 min (likely stuck)'
         ELSE 'under 30 min'
       END AS had_been_running,
       count(*) AS jobs, to_char(max(completed_at), 'DD-Mon-YYYY HH24:MI') AS last_seen
FROM bf_backfill_jobs
WHERE status = 'failed' AND error_message LIKE 'Interrupted by a server restart%'
GROUP BY 1
ORDER BY 1;

\echo
\echo '== F. Completed jobs that downloaded nothing'
SELECT source, timeframe, count(*) AS jobs, to_char(max(completed_at), 'DD-Mon-YYYY HH24:MI') AS latest
FROM bf_backfill_jobs
WHERE status = 'completed' AND downloaded_count = 0
GROUP BY source, timeframe
ORDER BY source, timeframe;

\echo
\echo '== G. Tracked symbols, and how many have no bars'
SELECT s.source, count(*) AS symbols,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id)) AS without_bars
FROM bf_symbols s
GROUP BY s.source
ORDER BY s.source;

\echo
\echo '== H. Stored backfill bars by source and timeframe'
SELECT s.source, b.timeframe, count(DISTINCT b.symbol_id) AS symbols, count(*) AS bars,
       to_char(min(b.ts), 'DD-Mon-YYYY') AS first_bar, to_char(max(b.ts), 'DD-Mon-YYYY HH24:MI') AS last_bar
FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
GROUP BY s.source, b.timeframe
ORDER BY s.source, b.timeframe;

\echo
\echo '== J. Backfilled but not yet copied to Charts/strategies (catalog sync backlog)'
WITH latest AS (
  SELECT symbol_id, max(completed_at) AS completed_at FROM bf_backfill_jobs WHERE status = 'completed' GROUP BY symbol_id
)
SELECT s.source, count(*) AS symbols_waiting, to_char(min(l.completed_at), 'DD-Mon HH24:MI') AS oldest_waiting_since
FROM latest l JOIN bf_symbols s ON s.id = l.symbol_id
WHERE s.last_synced_at IS NULL OR l.completed_at > s.last_synced_at
GROUP BY s.source
ORDER BY s.source;

\echo
\echo '== K. Backfilled bars missing from the main candle table (copy gaps)'
WITH bf AS (
  SELECT s.source, s.symbol, b.timeframe, count(*) AS backfilled
  FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
  GROUP BY s.source, s.symbol, b.timeframe
), main AS (
  SELECT i.exchange, i.symbol, c.timeframe, count(*) AS in_main
  FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  GROUP BY i.exchange, i.symbol, c.timeframe
)
SELECT bf.source, bf.timeframe, count(*) AS series,
       count(*) FILTER (WHERE coalesce(main.in_main, 0) < bf.backfilled) AS series_behind,
       coalesce(sum(greatest(bf.backfilled - coalesce(main.in_main, 0), 0)), 0) AS bars_missing
FROM bf
LEFT JOIN main ON main.symbol = bf.symbol AND main.timeframe = bf.timeframe
  AND main.exchange = CASE bf.source WHEN 'zerodha' THEN 'NSE' WHEN 'zerodha_nfo' THEN 'NFO' ELSE 'DELTA' END
GROUP BY bf.source, bf.timeframe
ORDER BY bf.source, bf.timeframe;

\echo
\echo '== L. Main candle table (what Charts and strategies read)'
SELECT i.exchange, c.timeframe, count(DISTINCT c.instrument_id) AS instruments, count(*) AS candles,
       to_char(max(c.ts), 'DD-Mon-YYYY HH24:MI') AS last_candle
FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
GROUP BY i.exchange, c.timeframe
ORDER BY i.exchange, c.timeframe;

\echo
\echo '== N. Delta 1m, last 7 days: backfill-table bars vs the chart table''s copy of the same minute'
\echo '   (chart rows written by the chart sync are finished candles; a differing'
\echo '    backfill bar was saved before its minute ended)'
SELECT c.source AS chart_row_written_by, count(*) AS compared,
       count(*) FILTER (WHERE b.close <> c.close OR b.high <> c.high OR b.low <> c.low
                        OR coalesce(b.volume, 0) <> coalesce(c.volume, 0)) AS differ
FROM bf_ohlcv_bars b
JOIN bf_symbols s ON s.id = b.symbol_id AND s.source = 'delta'
JOIN instruments i ON i.exchange = 'DELTA' AND i.symbol = s.symbol
JOIN ohlcv_candles c ON c.instrument_id = i.id AND c.timeframe = '1m' AND c.ts = b.ts
WHERE b.timeframe = '1m' AND b.ts > now() - interval '7 days'
GROUP BY c.source
ORDER BY c.source;

\echo
\echo '== O. Saved-up-to coverage (bf_coverage) and schedule'
SELECT (SELECT count(*) FROM bf_coverage) AS coverage_rows,
       to_char(coverage_built_at, 'DD-Mon HH24:MI') AS coverage_built, topup_time, live_start, live_end,
       auto_topup_zerodha, auto_topup_zerodha_nfo, delta_enabled
FROM bf_settings;

\echo
\echo '== P. Top-up runs, last 3 days'
SELECT kind, source, session_date, status, jobs_total, message,
       to_char(created_at, 'DD-Mon HH24:MI') AS created, to_char(completed_at, 'DD-Mon HH24:MI') AS completed
FROM bf_backfill_runs
WHERE created_at > now() - interval '3 days'
ORDER BY created_at DESC
LIMIT 20;

\echo
\echo '== Q. Failed in the last 3 hours, by source / timeframe / kind (counts; Kite error class only)'
SELECT source, timeframe,
       CASE
         WHEN error_message LIKE 'Interrupted by a server restart%' THEN 'interrupted by a server restart'
         WHEN error_message ILIKE '%does not support%' THEN 'timeframe not supported'
         WHEN error_message ILIKE '%paused%' THEN 'source paused'
         WHEN error_message ~ $re$API error '[A-Za-z]+'$re$ THEN 'Kite ' || substring(error_message from $re$API error '([A-Za-z]+)'$re$)
         WHEN error_message ILIKE '%not found%' OR error_message ILIKE '%no longer exists%' THEN 'symbol/instrument not found'
         WHEN error_message ILIKE '%no zerodha%' OR error_message ILIKE '%no credentials%' OR error_message ILIKE '%not authenticated%' THEN 'no Zerodha login'
         WHEN error_message ILIKE '%timeout%' OR error_message ILIKE '%timed out%' THEN 'timeout'
         ELSE 'other'
       END AS kind,
       CASE WHEN run_id IS NOT NULL THEN 'top-up' WHEN priority = 10 THEN 're-run/bulk' ELSE 'other' END AS job_type,
       count(*) AS jobs,
       to_char(min(created_at), 'DD-Mon') AS created_from,
       to_char(max(created_at), 'DD-Mon') AS created_to
FROM bf_backfill_jobs
WHERE status = 'failed' AND completed_at > now() - interval '3 hours'
GROUP BY 1, 2, 3, 4
ORDER BY jobs DESC
LIMIT 25;

\echo
\echo '== M. Database and table sizes'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows_estimate
FROM pg_stat_user_tables
WHERE relname IN ('bf_ohlcv_bars', 'bf_backfill_jobs', 'bf_symbols', 'ohlcv_candles', 'instruments')
ORDER BY pg_total_relation_size(relid) DESC;
