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
\echo '== R. NIFTY PCR health, next 4 expiries: contracts in the catalog, the latest 15m bucket, PCR as the app computes it (only contracts with a row in that bucket) vs every contract''s last OI on file'
WITH u AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50' ORDER BY created_at LIMIT 1),
exp AS (
  SELECT DISTINCT i.expiry FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY i.expiry LIMIT 4),
opt AS (
  SELECT i.id, i.expiry, i.option_type, i.strike FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry IN (SELECT expiry FROM exp)),
c AS (
  SELECT o.expiry, o.option_type, o.id, k.ts, k.open_interest
  FROM opt o JOIN ohlcv_candles k ON k.instrument_id = o.id AND k.timeframe = '15m'
  WHERE k.ts > now() - interval '10 days' AND k.open_interest IS NOT NULL),
latest AS (SELECT expiry, max(ts) AS ts FROM c GROUP BY expiry),
app AS (
  SELECT c.expiry, count(*) FILTER (WHERE option_type = 'CE') AS ce_rows, count(*) FILTER (WHERE option_type = 'PE') AS pe_rows,
         sum(open_interest) FILTER (WHERE option_type = 'CE') AS ce_oi, sum(open_interest) FILTER (WHERE option_type = 'PE') AS pe_oi
  FROM c JOIN latest l ON l.expiry = c.expiry AND l.ts = c.ts GROUP BY c.expiry),
lastoi AS (SELECT DISTINCT ON (id) expiry, option_type, open_interest FROM c ORDER BY id, ts DESC),
allc AS (
  SELECT expiry, count(*) FILTER (WHERE option_type = 'CE') AS ce_n, count(*) FILTER (WHERE option_type = 'PE') AS pe_n,
         sum(open_interest) FILTER (WHERE option_type = 'CE') AS ce_oi, sum(open_interest) FILTER (WHERE option_type = 'PE') AS pe_oi
  FROM lastoi GROUP BY expiry),
cat AS (
  SELECT expiry, count(*) FILTER (WHERE option_type = 'CE') AS ce, count(*) FILTER (WHERE option_type = 'PE') AS pe,
         min(strike) AS lo, max(strike) AS hi FROM opt GROUP BY expiry)
SELECT to_char(cat.expiry, 'DD-Mon') AS expiry, cat.ce AS catalog_ce, cat.pe AS catalog_pe, cat.lo AS min_strike, cat.hi AS max_strike,
       to_char(l.ts AT TIME ZONE 'Asia/Kolkata', 'DD-Mon HH24:MI') AS latest_bucket,
       app.ce_rows, app.pe_rows, round((app.pe_oi / NULLIF(app.ce_oi, 0))::numeric, 3) AS pcr_app,
       allc.ce_n AS ce_with_oi, allc.pe_n AS pe_with_oi, round((allc.pe_oi / NULLIF(allc.ce_oi, 0))::numeric, 3) AS pcr_last_oi,
       round(app.ce_oi) AS call_oi_app, round(app.pe_oi) AS put_oi_app, round(allc.ce_oi) AS call_oi_all, round(allc.pe_oi) AS put_oi_all
FROM cat LEFT JOIN latest l USING (expiry) LEFT JOIN app USING (expiry) LEFT JOIN allc USING (expiry)
ORDER BY cat.expiry;

\echo
\echo '== S. NIFTY next 4 expiries, today (IST) per 15m bucket: contracts with OI, rows written by the live feed, PCR over all 4 expiries in that bucket'
WITH u AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50' ORDER BY created_at LIMIT 1),
exp AS (
  SELECT DISTINCT i.expiry FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY i.expiry LIMIT 4),
opt AS (
  SELECT i.id, i.option_type FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry IN (SELECT expiry FROM exp))
SELECT to_char(k.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS bucket, count(*) AS contracts,
       count(*) FILTER (WHERE k.source = 'kite_live') AS from_live_feed,
       round((sum(k.open_interest) FILTER (WHERE o.option_type = 'PE') / NULLIF(sum(k.open_interest) FILTER (WHERE o.option_type = 'CE'), 0))::numeric, 3) AS pcr
FROM opt o JOIN ohlcv_candles k ON k.instrument_id = o.id AND k.timeframe = '15m'
WHERE k.open_interest IS NOT NULL
  AND k.ts >= date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
GROUP BY k.ts ORDER BY k.ts;

\echo
\echo '== T. Live-feed budget: unexpired NFO contracts the Kite ticker tries to subscribe (Kite allows 3,000 per connection)'
WITH u AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50' ORDER BY created_at LIMIT 1),
exp AS (
  SELECT DISTINCT i.expiry FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY i.expiry LIMIT 4)
SELECT CASE WHEN i.underlying_instrument_id = (SELECT id FROM u) AND i.expiry IN (SELECT expiry FROM exp) THEN 'NIFTY options, next 4 expiries'
            WHEN i.underlying_instrument_id = (SELECT id FROM u) THEN 'NIFTY, other'
            ELSE 'all other NFO' END AS contracts,
       count(*)
FROM instruments i
WHERE i.exchange = 'NFO' AND i.data_source = 'zerodha_kite' AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
GROUP BY 1 ORDER BY 1;

\echo
\echo '== U. NIFTY next 4 expiries, change-in-OI PCR (today vs previous session, app catalog strikes): sum of put OI change / sum of call OI change'
WITH u AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50' ORDER BY created_at LIMIT 1),
exp AS (
  SELECT DISTINCT i.expiry FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY i.expiry LIMIT 4),
opt AS (
  SELECT i.id, i.expiry, i.option_type FROM instruments i JOIN u ON i.underlying_instrument_id = u.id
  WHERE i.instrument_type = 'option' AND i.expiry IN (SELECT expiry FROM exp)),
c AS (
  SELECT o.id, o.expiry, o.option_type, k.ts, (k.ts AT TIME ZONE 'Asia/Kolkata')::date AS d, k.open_interest AS oi
  FROM opt o JOIN ohlcv_candles k ON k.instrument_id = o.id AND k.timeframe = '15m'
  WHERE k.ts > now() - interval '10 days' AND k.open_interest IS NOT NULL),
days AS (SELECT max(d) FILTER (WHERE d < (SELECT max(d) FROM c)) AS prev_d, max(d) AS cur_d FROM c),
cur AS (SELECT DISTINCT ON (id) id, expiry, option_type, oi FROM c WHERE d = (SELECT cur_d FROM days) ORDER BY id, ts DESC),
prev AS (SELECT DISTINCT ON (id) id, oi FROM c WHERE d = (SELECT prev_d FROM days) ORDER BY id, ts DESC),
j AS (SELECT cur.expiry, cur.option_type, cur.oi - coalesce(prev.oi, 0) AS chg, prev.id IS NULL AS no_prev FROM cur LEFT JOIN prev USING (id)),
per AS (
  SELECT to_char(expiry, 'DD-Mon') AS expiry, count(*) AS contracts, count(*) FILTER (WHERE no_prev) AS no_prev_day_oi,
         round(sum(chg) FILTER (WHERE option_type = 'CE')) AS call_oi_change, round(sum(chg) FILTER (WHERE option_type = 'PE')) AS put_oi_change,
         round((sum(chg) FILTER (WHERE option_type = 'PE') / NULLIF(sum(chg) FILTER (WHERE option_type = 'CE'), 0))::numeric, 3) AS coi_pcr,
         expiry AS sort_key
  FROM j GROUP BY expiry
  UNION ALL
  SELECT 'ALL 4', count(*), count(*) FILTER (WHERE no_prev),
         round(sum(chg) FILTER (WHERE option_type = 'CE')), round(sum(chg) FILTER (WHERE option_type = 'PE')),
         round((sum(chg) FILTER (WHERE option_type = 'PE') / NULLIF(sum(chg) FILTER (WHERE option_type = 'CE'), 0))::numeric, 3), '9999-12-31'::date
  FROM j)
SELECT (SELECT to_char(prev_d, 'DD-Mon') || ' -> ' || to_char(cur_d, 'DD-Mon') FROM days) AS sessions, expiry, contracts, no_prev_day_oi,
       call_oi_change, put_oi_change, coi_pcr
FROM per ORDER BY sort_key;

\echo
\echo '== V. Full NIFTY chain as tracked by the backfill (bf_symbols), next 4 expiries: strikes, OI coverage, full-chain PCR and change-in-OI PCR'
WITH exp AS (
  SELECT DISTINCT expiry FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND underlying_symbol = 'NIFTY' AND option_type IN ('CE', 'PE')
    AND expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY expiry LIMIT 4),
s AS (
  SELECT id, expiry, option_type, strike FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND underlying_symbol = 'NIFTY' AND option_type IN ('CE', 'PE') AND expiry IN (SELECT expiry FROM exp)),
b AS (
  SELECT s.id, s.expiry, s.option_type, x.ts, (x.ts AT TIME ZONE 'Asia/Kolkata')::date AS d, x.open_interest AS oi
  FROM s JOIN bf_ohlcv_bars x ON x.symbol_id = s.id AND x.timeframe = '15m'
  WHERE x.ts > now() - interval '10 days' AND x.open_interest IS NOT NULL),
days AS (SELECT max(d) FILTER (WHERE d < (now() AT TIME ZONE 'Asia/Kolkata')::date) AS prev_d, (now() AT TIME ZONE 'Asia/Kolkata')::date AS cur_d FROM b),
cur AS (SELECT DISTINCT ON (id) id, expiry, option_type, oi FROM b WHERE d = (SELECT cur_d FROM days) ORDER BY id, ts DESC),
prev AS (SELECT DISTINCT ON (id) id, expiry, option_type, oi FROM b WHERE d = (SELECT prev_d FROM days) ORDER BY id, ts DESC),
chain AS (SELECT expiry, count(*) FILTER (WHERE option_type = 'CE') AS ce, count(*) FILTER (WHERE option_type = 'PE') AS pe, min(strike) AS lo, max(strike) AS hi FROM s GROUP BY expiry),
pc AS (SELECT expiry, count(*) AS n, sum(oi) FILTER (WHERE option_type = 'CE') AS c_oi, sum(oi) FILTER (WHERE option_type = 'PE') AS p_oi FROM prev GROUP BY expiry),
cc AS (SELECT expiry, count(*) AS n, sum(oi) FILTER (WHERE option_type = 'CE') AS c_oi, sum(oi) FILTER (WHERE option_type = 'PE') AS p_oi FROM cur GROUP BY expiry),
chg AS (SELECT cur.expiry, cur.option_type, cur.oi - prev.oi AS d_oi FROM cur JOIN prev USING (id)),
ch AS (SELECT expiry, count(*) AS n, sum(d_oi) FILTER (WHERE option_type = 'CE') AS c_d, sum(d_oi) FILTER (WHERE option_type = 'PE') AS p_d FROM chg GROUP BY expiry)
SELECT to_char(chain.expiry, 'DD-Mon') AS expiry, chain.ce AS chain_ce, chain.pe AS chain_pe, chain.lo AS min_strike, chain.hi AS max_strike,
       pc.n AS prev_day_with_oi, round((pc.p_oi / NULLIF(pc.c_oi, 0))::numeric, 3) AS pcr_prev_close,
       cc.n AS today_with_oi, round((cc.p_oi / NULLIF(cc.c_oi, 0))::numeric, 3) AS pcr_today,
       ch.n AS both_days, round(ch.c_d) AS call_oi_change, round(ch.p_d) AS put_oi_change,
       round((ch.p_d / NULLIF(ch.c_d, 0))::numeric, 3) AS coi_pcr
FROM chain LEFT JOIN pc USING (expiry) LEFT JOIN cc USING (expiry) LEFT JOIN ch USING (expiry)
ORDER BY chain.expiry;

\echo
\echo '== V2. Same, all 4 expiries summed (full chain as tracked by the backfill)'
WITH exp AS (
  SELECT DISTINCT expiry FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND underlying_symbol = 'NIFTY' AND option_type IN ('CE', 'PE')
    AND expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY expiry LIMIT 4),
s AS (
  SELECT id, option_type FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND underlying_symbol = 'NIFTY' AND option_type IN ('CE', 'PE') AND expiry IN (SELECT expiry FROM exp)),
b AS (
  SELECT s.id, s.option_type, x.ts, (x.ts AT TIME ZONE 'Asia/Kolkata')::date AS d, x.open_interest AS oi
  FROM s JOIN bf_ohlcv_bars x ON x.symbol_id = s.id AND x.timeframe = '15m'
  WHERE x.ts > now() - interval '10 days' AND x.open_interest IS NOT NULL),
days AS (SELECT max(d) FILTER (WHERE d < (now() AT TIME ZONE 'Asia/Kolkata')::date) AS prev_d, (now() AT TIME ZONE 'Asia/Kolkata')::date AS cur_d FROM b),
cur AS (SELECT DISTINCT ON (id) id, option_type, oi FROM b WHERE d = (SELECT cur_d FROM days) ORDER BY id, ts DESC),
prev AS (SELECT DISTINCT ON (id) id, option_type, oi FROM b WHERE d = (SELECT prev_d FROM days) ORDER BY id, ts DESC),
chg AS (SELECT cur.option_type, cur.oi - prev.oi AS d_oi FROM cur JOIN prev USING (id))
SELECT (SELECT count(*) FROM s) AS chain_contracts,
       (SELECT count(*) FROM prev) AS prev_day_with_oi,
       (SELECT round((sum(oi) FILTER (WHERE option_type = 'PE') / NULLIF(sum(oi) FILTER (WHERE option_type = 'CE'), 0))::numeric, 3) FROM prev) AS pcr_prev_close,
       (SELECT count(*) FROM cur) AS today_with_oi,
       (SELECT round((sum(oi) FILTER (WHERE option_type = 'PE') / NULLIF(sum(oi) FILTER (WHERE option_type = 'CE'), 0))::numeric, 3) FROM cur) AS pcr_today,
       (SELECT count(*) FROM chg) AS both_days,
       (SELECT round(sum(d_oi) FILTER (WHERE option_type = 'CE')) FROM chg) AS call_oi_change,
       (SELECT round(sum(d_oi) FILTER (WHERE option_type = 'PE')) FROM chg) AS put_oi_change,
       (SELECT round((sum(d_oi) FILTER (WHERE option_type = 'PE') / NULLIF(sum(d_oi) FILTER (WHERE option_type = 'CE'), 0))::numeric, 3) FROM chg) AS coi_pcr;

\echo
\echo '== W. 15-minute PCR records (27 a session, 09:00-15:30 IST): last 5 sessions'
SELECT session_date, count(*) AS records,
       count(*) FILTER (WHERE source = 'live_quote') AS live,
       count(*) FILTER (WHERE source = 'historical_fill') AS filled,
       to_char(min(ts AT TIME ZONE 'Asia/Kolkata'), 'HH24:MI') AS first_mark,
       to_char(max(ts AT TIME ZONE 'Asia/Kolkata'), 'HH24:MI') AS last_mark,
       min(round(100.0 * contracts_with_oi / NULLIF(contracts_expected, 0))) AS min_coverage_pct,
       count(*) FILTER (WHERE flags::text LIKE '%late%') AS late,
       count(*) FILTER (WHERE flags::text LIKE '%gap_before%') AS gap_before,
       count(*) FILTER (WHERE flags::text LIKE '%low_coverage%') AS low_coverage
FROM pcr_snapshots WHERE underlying = 'NIFTY'
GROUP BY session_date ORDER BY session_date DESC LIMIT 5;

\echo
\echo '== W2. Latest 5 PCR records'
SELECT to_char(ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS mark_ist, source,
       round(extract(epoch FROM captured_at - ts)) AS capture_delay_s,
       round(spot::numeric, 1) AS nifty, atm_strike, contracts_with_oi || '/' || contracts_expected AS coverage,
       round(pcr::numeric, 3) AS pcr, round(pcr_change::numeric, 3) AS pcr_change,
       round(call_oi_change) AS call_oi_change, round(put_oi_change) AS put_oi_change,
       round(oi_change_pcr::numeric, 2) AS oi_change_pcr, positioning
FROM pcr_snapshots WHERE underlying = 'NIFTY'
ORDER BY ts DESC LIMIT 5;

\echo
\echo '== W3. PCR records of the latest session, every mark'
SELECT to_char(ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS mark_ist, source, round(spot::numeric, 1) AS nifty, atm_strike,
       contracts_with_oi || '/' || contracts_expected AS coverage, round(pcr::numeric, 3) AS pcr, positioning
FROM pcr_snapshots
WHERE underlying = 'NIFTY' AND session_date = (SELECT max(session_date) FROM pcr_snapshots WHERE underlying = 'NIFTY')
ORDER BY ts;

\echo
\echo '== W4. NIFTY 50 15m candles saved by the backfill for that session (reference for W3)'
SELECT to_char(x.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS candle_start, round(x.open::numeric, 1) AS open, round(x.close::numeric, 1) AS close
FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
WHERE s.symbol = 'NIFTY 50' AND x.timeframe = '15m'
  AND (x.ts AT TIME ZONE 'Asia/Kolkata')::date = (SELECT max(session_date) FROM pcr_snapshots WHERE underlying = 'NIFTY')
ORDER BY x.ts;

\echo
\echo '== W6. NIFTY in the PCR records vs the backfill 15m candles, per session'
WITH p AS (
  SELECT session_date, count(DISTINCT spot) AS distinct_spot, min(spot) AS pcr_min, max(spot) AS pcr_max,
         min(atm_strike) AS atm_min, max(atm_strike) AS atm_max
  FROM pcr_snapshots WHERE underlying = 'NIFTY' GROUP BY session_date),
b AS (
  SELECT (x.ts AT TIME ZONE 'Asia/Kolkata')::date AS d, count(*) AS candles, min(x.close) AS bf_min, max(x.close) AS bf_max,
         (array_agg(x.close ORDER BY x.ts DESC))[1] AS bf_last_close
  FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
  WHERE s.symbol = 'NIFTY 50' AND x.timeframe = '15m' AND x.ts > now() - interval '10 days'
  GROUP BY 1)
SELECT b.d AS session, p.distinct_spot, round(p.pcr_min::numeric, 1) AS pcr_nifty_min, round(p.pcr_max::numeric, 1) AS pcr_nifty_max,
       p.atm_min, p.atm_max, b.candles AS bf_candles, round(b.bf_min::numeric, 1) AS bf_min, round(b.bf_max::numeric, 1) AS bf_max,
       round(b.bf_last_close::numeric, 1) AS bf_last_close
FROM b LEFT JOIN p ON p.session_date = b.d ORDER BY b.d;

\echo
\echo '== W5. Latest PCR record: contracts without OI, by expiry and distance from ATM'
SELECT o.expiry, count(*) AS contracts, count(*) FILTER (WHERE o.oi IS NULL) AS no_oi,
       count(*) FILTER (WHERE o.oi IS NULL AND abs(o.strike - p.atm_strike) <= 500) AS no_oi_within_500,
       count(*) FILTER (WHERE o.oi IS NULL AND abs(o.strike - p.atm_strike) > 1500) AS no_oi_beyond_1500,
       min(o.strike) AS min_strike, max(o.strike) AS max_strike
FROM pcr_strike_oi o JOIN pcr_snapshots p ON p.id = o.snapshot_id
WHERE p.id = (SELECT id FROM pcr_snapshots WHERE underlying = 'NIFTY' ORDER BY ts DESC LIMIT 1)
GROUP BY o.expiry ORDER BY o.expiry;

\echo
\echo '== X. Timeframes in use: strategies and deployments (counts), and 1m data held'
SELECT 'strategy versions' AS what, timeframe, count(*) AS n FROM strategy_versions GROUP BY timeframe
UNION ALL SELECT 'paper deployments (' || status || ')', timeframe, count(*) FROM paper_deployments GROUP BY status, timeframe
UNION ALL SELECT 'live deployments (' || status || ')', timeframe, count(*) FROM live_deployments GROUP BY status, timeframe
ORDER BY 1, 2;
SELECT 'bf_ohlcv_bars' AS table_name, count(*) AS rows_1m FROM bf_ohlcv_bars WHERE timeframe = '1m'
UNION ALL SELECT 'ohlcv_candles', count(*) FROM ohlcv_candles WHERE timeframe = '1m'
UNION ALL SELECT 'bf_coverage', count(*) FROM bf_coverage WHERE timeframe = '1m'
UNION ALL SELECT 'bf_backfill_jobs (pending/running)', count(*) FROM bf_backfill_jobs WHERE timeframe = '1m' AND status IN ('pending', 'running');
SELECT topup_timeframes FROM bf_settings;

\echo
\echo '== Y. Storage by data set and timeframe: rows and MB in total, and per trading day (last 7 days)'
\echo '   (every candle is kept twice: bf_ohlcv_bars (backfill store) and ohlcv_candles (charts/strategies);'
\echo '    MB = rows x measured bytes per row of each table, indexes included)'
WITH sz AS (
  SELECT relname, pg_total_relation_size(relid)::numeric / NULLIF(n_live_tup, 0) AS bpr
  FROM pg_stat_user_tables WHERE relname IN ('bf_ohlcv_bars', 'ohlcv_candles')),
bf AS (
  SELECT CASE
           WHEN s.source = 'zerodha' THEN '1 NSE equities + indices'
           WHEN s.source = 'delta' THEN '6 Delta crypto'
           WHEN s.option_type IS NULL THEN '5 NFO futures'
           WHEN s.underlying_symbol = 'NIFTY' THEN '2 NFO NIFTY options'
           WHEN s.underlying_symbol IN ('BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX') THEN '3 NFO other index options'
           ELSE '4 NFO stock options' END AS dataset,
         x.timeframe,
         count(DISTINCT x.symbol_id) AS symbols,
         count(*) AS rows_total,
         count(*) FILTER (WHERE x.ts > now() - interval '7 days') AS rows_7d,
         count(DISTINCT (x.ts AT TIME ZONE 'Asia/Kolkata')::date) FILTER (WHERE x.ts > now() - interval '7 days') AS days_7d
  FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
  GROUP BY 1, 2),
mc AS (
  SELECT CASE
           WHEN i.exchange = 'NSE' THEN '1 NSE equities + indices'
           WHEN i.exchange = 'DELTA' THEN '6 Delta crypto'
           WHEN i.option_type IS NULL THEN '5 NFO futures'
           WHEN u.symbol = 'NIFTY 50' THEN '2 NFO NIFTY options'
           WHEN u.symbol LIKE 'NIFTY%' OR u.symbol IN ('SENSEX', 'BANKEX') THEN '3 NFO other index options'
           ELSE '4 NFO stock options' END AS dataset,
         c.timeframe,
         count(*) AS rows_total,
         count(*) FILTER (WHERE c.ts > now() - interval '7 days') AS rows_7d
  FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  LEFT JOIN instruments u ON u.id = i.underlying_instrument_id
  GROUP BY 1, 2)
SELECT coalesce(bf.dataset, mc.dataset) AS dataset, coalesce(bf.timeframe, mc.timeframe) AS tf,
       bf.symbols,
       coalesce(bf.rows_total, 0) + coalesce(mc.rows_total, 0) AS rows_total,
       round((coalesce(bf.rows_total, 0) * (SELECT bpr FROM sz WHERE relname = 'bf_ohlcv_bars')
            + coalesce(mc.rows_total, 0) * (SELECT bpr FROM sz WHERE relname = 'ohlcv_candles')) / 1e6) AS mb_total,
       bf.days_7d,
       round((coalesce(bf.rows_7d, 0) + coalesce(mc.rows_7d, 0))::numeric / NULLIF(bf.days_7d, 0)) AS rows_per_day,
       round(((coalesce(bf.rows_7d, 0) * (SELECT bpr FROM sz WHERE relname = 'bf_ohlcv_bars')
             + coalesce(mc.rows_7d, 0) * (SELECT bpr FROM sz WHERE relname = 'ohlcv_candles')) / NULLIF(bf.days_7d, 0) / 1e6)::numeric, 1) AS mb_per_day
FROM bf FULL JOIN mc ON mc.dataset = bf.dataset AND mc.timeframe = bf.timeframe
ORDER BY 1, 2;

\echo
\echo '== Y2. Bytes per row, and the rest of the database'
SELECT relname AS table_name, n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) AS size,
       round(pg_total_relation_size(relid)::numeric / NULLIF(n_live_tup, 0)) AS bytes_per_row,
       pg_size_pretty(pg_indexes_size(relid)) AS of_which_indexes
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 12;
SELECT pg_size_pretty(pg_database_size(current_database())) AS database,
       pg_size_pretty(sum(pg_total_relation_size(relid)) FILTER (WHERE relname NOT IN ('bf_ohlcv_bars', 'ohlcv_candles'))) AS everything_else
FROM pg_stat_user_tables;

\echo
\echo '== Y3. PCR records per trading day'
SELECT count(DISTINCT p.session_date) AS sessions, count(DISTINCT p.id) AS records, count(o.id) AS contract_rows,
       pg_size_pretty(pg_total_relation_size('pcr_strike_oi') + pg_total_relation_size('pcr_snapshots') + pg_total_relation_size('pcr_snapshot_expiries')) AS size,
       pg_size_pretty(((pg_total_relation_size('pcr_strike_oi') + pg_total_relation_size('pcr_snapshots') + pg_total_relation_size('pcr_snapshot_expiries'))
                       / NULLIF(count(DISTINCT p.session_date), 0))::bigint) AS per_session
FROM pcr_snapshots p LEFT JOIN pcr_strike_oi o ON o.snapshot_id = p.id;

\echo
\echo '== Z. Stock options per stock (F&O opening momentum, Step 3 Total OI): contracts per stock, current month vs all live expiries'
WITH so AS (
  SELECT s.underlying_symbol AS u, s.expiry FROM bf_symbols s
  WHERE s.source = 'zerodha_nfo' AND s.option_type IN ('CE', 'PE')
    AND s.underlying_symbol NOT IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX', 'BANKEX')),
cur AS (SELECT u, min(expiry) AS e FROM so WHERE expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date GROUP BY u),
per AS (
  SELECT so.u, count(*) FILTER (WHERE so.expiry = cur.e) AS cur_contracts,
         count(*) FILTER (WHERE so.expiry >= cur.e) AS live_contracts,
         count(DISTINCT so.expiry) FILTER (WHERE so.expiry >= cur.e) AS live_expiries,
         count(*) FILTER (WHERE so.expiry < cur.e) AS expired_contracts
  FROM so JOIN cur USING (u) GROUP BY so.u)
SELECT count(*) AS stocks,
       round(avg(cur_contracts)) AS avg_current_month, percentile_disc(0.5) WITHIN GROUP (ORDER BY cur_contracts) AS median_current_month,
       min(cur_contracts) AS min_current_month, max(cur_contracts) AS max_current_month,
       round(avg(live_contracts)) AS avg_all_live, max(live_expiries) AS live_expiries, sum(expired_contracts) AS expired_contracts_kept
FROM per;

\echo
\echo '== F1. F&O opening momentum (FLY OI SCN): saved code versions and deployments (repo file md5 806e4008398d0517889dcd0ce7003c31)'
SELECT sv.version_number, (sv.created_at AT TIME ZONE 'Asia/Kolkata')::date AS saved_on,
       md5(replace(sv.python_code, E'\r', '')) = '806e4008398d0517889dcd0ce7003c31' AS same_as_repo,
       sv.python_code LIKE '%_total_oi_pct_change%' AS total_oi_gate, sv.python_code LIKE '%second_scan_done%' AS scan_925,
       sv.python_code LIKE '%VERSION = 6%' AS is_v6, sv.python_code LIKE '%0, rather than being excluded%' AS missing_oi_as_zero,
       sv.python_code LIKE '%F&O Spurt%' AS titled_spurt, length(sv.python_code) AS code_chars,
       count(d.id) AS deployments, count(d.id) FILTER (WHERE d.status = 'active') AS active,
       max(d.last_evaluated_at) AS last_evaluated
FROM strategy_versions sv LEFT JOIN paper_native_deployments d ON d.strategy_version_id = sv.id
WHERE (sv.python_code LIKE '%FLY OI SCN%' OR sv.python_code LIKE '%F&O Opening-Candle Momentum Scanner%')
GROUP BY sv.id, sv.version_number, sv.created_at, sv.python_code ORDER BY sv.created_at;

\echo
\echo '== F2. Its last session per deployment: 9:20/9:25 scan counts, rejection reasons, setup outcomes, OI legs counted'
SELECT d.status, d.state->>'session_date' AS session,
       d.state->'scan_log'->'9:20'->>'data_source' LIKE 'Kite live quotes%' AS live_quotes_920,
       (d.state->'scan_log'->'9:20'->'counts'->>'scanned')::int AS scanned,
       (d.state->'scan_log'->'9:20'->'counts'->>'below_momentum')::int AS below_2pct,
       (d.state->'scan_log'->'9:20'->'counts'->>'no_price')::int AS no_price,
       json_array_length(d.state->'scan_log'->'9:20'->'shortlisted') AS shortlisted_920,
       json_array_length(d.state->'scan_log'->'9:25'->'shortlisted') AS shortlisted_925,
       json_array_length(d.state->'scan_log'->'9:20'->'rejected') AS rejected_920,
       (SELECT count(*) FROM json_array_elements(d.state->'scan_log'->'9:20'->'rejected') r WHERE r::text LIKE '%no Total OI baseline%') AS rej_no_oi_baseline,
       (SELECT count(*) FROM json_array_elements(d.state->'scan_log'->'9:20'->'rejected') r WHERE r::text LIKE '%needs beyond%') AS rej_oi_below_7pct,
       (SELECT count(*) FROM json_array_elements(d.state->'scan_log'->'9:20'->'rejected') r WHERE r::text LIKE '%retraced%') AS rej_retraced,
       (SELECT count(*) FROM json_array_elements(d.state->'scan_log'->'9:20'->'rejected') r WHERE r::text LIKE '%Nifty 9:15-9:20 candle red%') AS rej_nifty_red,
       (SELECT count(*) FROM json_each(d.state->'setups') s WHERE s.value->>'status' IN ('triggered', 'exited', 'eod_closed')) AS setups_triggered,
       (SELECT count(*) FROM json_each(d.state->'setups') s WHERE s.value->>'status' = 'no_trigger') AS setups_no_trigger,
       (SELECT count(*) FROM json_each(d.state->'setups') s WHERE s.value->>'status' = 'blackout') AS setups_blackout,
       (SELECT sum((s.value->'oi_detail'->>'ce_counted')::int) || '/' || sum((s.value->'oi_detail'->>'ce_listed')::int) FROM json_each(d.state->'setups') s) AS ce_legs_counted,
       (SELECT sum((s.value->'oi_detail'->>'pe_counted')::int) || '/' || sum((s.value->'oi_detail'->>'pe_listed')::int) FROM json_each(d.state->'setups') s) AS pe_legs_counted
FROM paper_native_deployments d JOIN strategy_versions sv ON sv.id = d.strategy_version_id
WHERE (sv.python_code LIKE '%FLY OI SCN%' OR sv.python_code LIKE '%F&O Opening-Candle Momentum Scanner%')
ORDER BY d.last_evaluated_at DESC NULLS LAST;

\echo
\echo '== F3. Its alerts per day (last 20 days): scan summary numbers, per-stock shortlists, option OI legs counted in them, trade closes'
WITH a AS (
  SELECT (al.created_at AT TIME ZONE 'Asia/Kolkata')::date AS day, al.title, al.message, al.alert_type
  FROM alerts al JOIN paper_native_deployments d ON al.object_type = 'paper_native_deployment' AND al.object_id = d.id::text
  JOIN strategy_versions sv ON sv.id = d.strategy_version_id
  WHERE (sv.python_code LIKE '%FLY OI SCN%' OR sv.python_code LIKE '%F&O Opening-Candle Momentum Scanner%') AND al.created_at > now() - interval '20 days')
SELECT day,
       max((regexp_match(message, 'Scanned (\d+) F&O stocks'))[1]::int) AS scanned,
       max((regexp_match(message, 'Below the >2% move: (\d+)'))[1]::int) AS below_2pct,
       bool_or(message LIKE '%data: Kite live quotes%') AS live_quotes,
       count(*) FILTER (WHERE title ~ 'shortlisted at' AND title !~ ': \d+ (more )?shortlisted at') AS stock_shortlists,
       max((regexp_match(message, 'rejected \((\d+)\):'))[1]::int) AS rejected_after_2pct,
       sum((regexp_match(message, 'CE \((\d+)/\d+ strikes\)'))[1]::int) AS ce_counted, sum((regexp_match(message, 'CE \(\d+/(\d+) strikes\)'))[1]::int) AS ce_listed,
       sum((regexp_match(message, 'PE \((\d+)/\d+ strikes\)'))[1]::int) AS pe_counted, sum((regexp_match(message, 'PE \(\d+/(\d+) strikes\)'))[1]::int) AS pe_listed,
       count(*) FILTER (WHERE message ~ 'CE \(all strikes\): 0 ->') AS ce_yesterday_zero, count(*) FILTER (WHERE message ~ 'PE \(all strikes\): 0 ->') AS pe_yesterday_zero,
       count(*) FILTER (WHERE message ~ 'Futures: 0 ->') AS fut_yesterday_zero,
       min((regexp_match(message, '\(([+-][0-9.]+)%, threshold >7%\)'))[1]::numeric) AS min_oi_pct,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY (regexp_match(message, '\(([+-][0-9.]+)%, threshold >7%\)'))[1]::numeric) AS median_oi_pct,
       max((regexp_match(message, '\(([+-][0-9.]+)%, threshold >7%\)'))[1]::numeric) AS max_oi_pct,
       count(*) FILTER (WHERE title LIKE '% closed') AS closes, count(*) FILTER (WHERE title LIKE '%3:10pm report') AS reports
FROM a GROUP BY day ORDER BY day;

\echo
\echo '== F4. Its closed paper trades (counts only)'
SELECT count(*) AS trades, count(*) FILTER (WHERE t.pnl > 0) AS wins, count(*) FILTER (WHERE t.pnl < 0) AS losses,
       count(*) FILTER (WHERE t.exit_reason LIKE '%SMA%') AS sma_exits, count(*) FILTER (WHERE t.exit_reason LIKE '3:10pm%') AS exits_310pm,
       count(*) FILTER (WHERE t.exit_reason = 'manual') AS manual_exits,
       count(DISTINCT (t.opened_at AT TIME ZONE 'Asia/Kolkata')::date) AS trade_days,
       min((t.opened_at AT TIME ZONE 'Asia/Kolkata')::date) AS first_trade, max((t.opened_at AT TIME ZONE 'Asia/Kolkata')::date) AS last_trade
FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id
JOIN strategy_versions sv ON sv.id = d.strategy_version_id
WHERE (sv.python_code LIKE '%FLY OI SCN%' OR sv.python_code LIKE '%F&O Opening-Candle Momentum Scanner%');

\echo
\echo '== F5. What the 9:20 scan could see, per session: candles it needs, when they were saved, and yesterday OI on file (counts only)'
WITH nifty AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50' AND exchange = 'NSE' ORDER BY id LIMIT 1),
days AS (
  SELECT d, prev FROM (
    SELECT d, lag(d) OVER (ORDER BY d) AS prev FROM (
      SELECT DISTINCT (c.ts AT TIME ZONE 'Asia/Kolkata')::date AS d FROM ohlcv_candles c JOIN nifty ON c.instrument_id = nifty.id
      WHERE c.timeframe = '5m' AND c.ts > now() - interval '12 days') x) y
  WHERE prev IS NOT NULL ORDER BY d DESC LIMIT 4),
fut AS (
  SELECT DISTINCT ON (i.underlying_instrument_id) i.underlying_instrument_id AS eq, i.id AS fut_id, i.expiry
  FROM instruments i WHERE i.instrument_type = 'future' AND i.underlying_instrument_id IS NOT NULL
    AND i.expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY i.underlying_instrument_id, i.expiry),
opt AS (SELECT o.id FROM instruments o JOIN fut ON o.underlying_instrument_id = fut.eq AND o.expiry = fut.expiry WHERE o.instrument_type = 'option')
SELECT days.d AS session,
  (SELECT to_char(min(c.created_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') FROM ohlcv_candles c JOIN nifty ON c.instrument_id = nifty.id
     WHERE c.timeframe = '5m' AND c.ts = (days.d + time '09:15') AT TIME ZONE 'Asia/Kolkata') AS nifty_915_saved_at,
  (SELECT count(*) FROM fut) AS fno_stocks,
  (SELECT count(*) FROM fut JOIN ohlcv_candles c ON c.instrument_id = fut.eq AND c.timeframe = '5m'
     AND c.ts = (days.d + time '09:15') AT TIME ZONE 'Asia/Kolkata'
     WHERE c.created_at < (days.d + time '09:21') AT TIME ZONE 'Asia/Kolkata') AS stock_915_saved_by_921,
  (SELECT count(*) FROM fut JOIN ohlcv_candles c ON c.instrument_id = fut.eq AND c.timeframe = '5m'
     AND c.ts = (days.d + time '09:15') AT TIME ZONE 'Asia/Kolkata') AS stock_915_saved_ever,
  (SELECT count(*) FILTER (WHERE abs(mv) > 0.02) || ' of ' || count(*) || ', >1%: ' || count(*) FILTER (WHERE abs(mv) > 0.01) || ', max ' || round((max(abs(mv)) * 100)::numeric, 1) || '%'
     FROM (SELECT t.close / NULLIF(p.close, 0) - 1 AS mv FROM fut
       JOIN ohlcv_candles t ON t.instrument_id = fut.eq AND t.timeframe = '5m' AND t.ts = (days.d + time '09:15') AT TIME ZONE 'Asia/Kolkata'
       CROSS JOIN LATERAL (SELECT c.close FROM ohlcv_candles c WHERE c.instrument_id = fut.eq AND c.timeframe = '5m'
                             AND c.ts < (days.d + time '09:15') AT TIME ZONE 'Asia/Kolkata' ORDER BY c.ts DESC LIMIT 1) p) m) AS moved_over_2pct_at_920,
  (SELECT count(*) FROM fut JOIN ohlcv_candles c ON c.instrument_id = fut.fut_id AND c.timeframe = '1d'
     AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = days.prev AND c.open_interest IS NOT NULL) AS futures_with_yesterday_oi,
  (SELECT count(*) FROM opt) AS current_month_options,
  (SELECT count(*) FROM opt JOIN ohlcv_candles c ON c.instrument_id = opt.id AND c.timeframe = '1d'
     AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = days.prev) AS options_with_yesterday_oi
FROM days ORDER BY days.d;

\echo
\echo '== F6. How soon closed 5m candles reached the database (saved within 10 minutes of closing, last 8 days): delay after the candle closed, seconds'
WITH c AS (
  SELECT (c.ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
         extract(epoch FROM c.created_at - (c.ts + interval '5 minutes')) AS delay_s,
         (c.ts AT TIME ZONE 'Asia/Kolkata')::time = time '09:15' AS first_candle
  FROM ohlcv_candles c
  WHERE c.timeframe = '5m' AND c.ts > now() - interval '8 days'
    AND c.created_at >= c.ts + interval '5 minutes' AND c.created_at < c.ts + interval '15 minutes')
SELECT day, count(*) AS candles, round(min(delay_s)::numeric, 1) AS min_s,
       round(percentile_cont(0.1) WITHIN GROUP (ORDER BY delay_s)::numeric, 1) AS p10_s,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_s)::numeric, 1) AS median_s,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY delay_s)::numeric, 1) AS p90_s,
       count(*) FILTER (WHERE first_candle) AS first_candles, round(min(delay_s) FILTER (WHERE first_candle)::numeric, 1) AS first_candle_min_s
FROM c GROUP BY day ORDER BY day;

\echo
\echo '== F7. FLY OI SCN v6 data: OI store readings per session and mark, scan results per session and scan (counts and times only)'
SELECT to_regclass('public.fo_oi_snapshots') IS NOT NULL AS has_fo_scan \gset
\if :has_fo_scan
SELECT session_date, mark, source, count(*) AS contracts, count(oi) AS with_oi, count(DISTINCT underlying_id) AS stocks,
       to_char(min(captured_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS first_saved,
       to_char(max(captured_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS last_saved
FROM fo_oi_snapshots GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
SELECT count(*) AS total_rows_kept, count(DISTINCT session_date) AS sessions, min(session_date) AS since FROM fo_oi_totals;
SELECT session_date, scan, count(*) AS stocks, count(*) FILTER (WHERE passed_move) AS moved_2pct,
       count(*) FILTER (WHERE passed_oi) AS oi_rise_7pct, count(*) FILTER (WHERE passed_retrace) AS candle_ok,
       count(*) FILTER (WHERE outcome NOT IN ('rejected', 'pending')) AS listed,
       count(*) FILTER (WHERE outcome IN ('triggered', 'exited', 'eod_closed')) AS traded,
       count(*) FILTER (WHERE oi_baseline = 'close') AS base_close, count(*) FILTER (WHERE oi_baseline = 'pre_open') AS base_pre_open,
       count(*) FILTER (WHERE oi_baseline = 'daily_candle') AS base_daily, count(*) FILTER (WHERE passed_move AND oi_baseline IS NULL) AS base_missing,
       to_char(min(scanned_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS scan_started,
       to_char(max(scanned_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS scan_saved
FROM fo_scan_results GROUP BY 1, 2 ORDER BY 1, 2;
\else
\echo '(not deployed yet)'
\endif

\echo
\echo '== G. Stock-option candles still held (index options excluded): rows by copy and timeframe, estimated size'
WITH so AS (
  SELECT id, symbol FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND option_type IN ('CE', 'PE')
    AND (underlying_symbol IS NULL OR underlying_symbol NOT IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50'))),
si AS (SELECT i.id FROM instruments i JOIN so ON i.external_ref = so.symbol WHERE i.exchange = 'NFO' AND i.instrument_type = 'option')
SELECT (SELECT count(*) FROM so) AS stock_option_symbols,
       (SELECT count(*) FROM bf_symbols WHERE source = 'zerodha_nfo' AND option_type IN ('CE', 'PE') AND underlying_symbol IS NULL) AS without_underlying,
       (SELECT count(*) FROM si) AS matching_instruments,
       (SELECT count(*) FROM bf_coverage v JOIN so ON v.symbol_id = so.id) AS coverage_rows;
WITH so AS (
  SELECT id, symbol FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND option_type IN ('CE', 'PE')
    AND (underlying_symbol IS NULL OR underlying_symbol NOT IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50'))),
si AS (SELECT i.id FROM instruments i JOIN so ON i.external_ref = so.symbol WHERE i.exchange = 'NFO' AND i.instrument_type = 'option')
SELECT 'backfill copy' AS copy, b.timeframe, count(*) AS rows,
       pg_size_pretty((count(*) * pg_total_relation_size('bf_ohlcv_bars') / NULLIF((SELECT reltuples FROM pg_class WHERE relname = 'bf_ohlcv_bars'), 0))::bigint) AS est_size
FROM bf_ohlcv_bars b JOIN so ON b.symbol_id = so.id GROUP BY b.timeframe
UNION ALL
SELECT 'chart copy', c.timeframe, count(*),
       pg_size_pretty((count(*) * pg_total_relation_size('ohlcv_candles') / NULLIF((SELECT reltuples FROM pg_class WHERE relname = 'ohlcv_candles'), 0))::bigint)
FROM ohlcv_candles c JOIN si ON c.instrument_id = si.id GROUP BY c.timeframe
ORDER BY 1, 2;
SELECT coalesce(u.instrument_type, '(none)') AS option_underlying_type, count(*) AS options
FROM instruments o LEFT JOIN instruments u ON u.id = o.underlying_instrument_id WHERE o.instrument_type = 'option' GROUP BY 1 ORDER BY 1;

\echo
\echo '== U. Space inside the candle tables: live rows, deleted rows not yet vacuumed, space free for reuse (estimate: size minus live rows x bytes per row measured 26 Sep)'
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS live_rows, n_dead_tup AS deleted_not_vacuumed,
       pg_size_pretty(greatest(pg_total_relation_size(relid) - n_live_tup * CASE relname WHEN 'bf_ohlcv_bars' THEN 253 ELSE 277 END, 0)) AS est_free_for_reuse,
       to_char(greatest(last_autovacuum, last_vacuum) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_vacuum_ist
FROM pg_stat_user_tables WHERE relname IN ('bf_ohlcv_bars', 'ohlcv_candles') ORDER BY 1;

\echo
\echo '== H. Delta Exchange usage (counts only): instruments, deployments, backtests, watchlists, broker link, candles held'
WITH di AS (SELECT id, is_active FROM instruments WHERE exchange = 'DELTA' OR data_source = 'delta_exchange'),
ds AS (SELECT id FROM bf_symbols WHERE source = 'delta')
SELECT (SELECT count(*) FROM di) AS delta_instruments, (SELECT count(*) FROM di WHERE is_active) AS active_instruments,
       (SELECT count(*) FROM paper_deployments p JOIN di ON p.instrument_id = di.id) AS paper_deployments,
       (SELECT count(*) FROM paper_deployments p JOIN di ON p.instrument_id = di.id WHERE p.status = 'active') AS paper_active,
       (SELECT count(*) FROM live_deployments l JOIN di ON l.instrument_id = di.id) AS live_deployments,
       (SELECT count(*) FROM live_deployments l JOIN di ON l.instrument_id = di.id WHERE l.status = 'active') AS live_active,
       (SELECT count(*) FROM backtest_jobs b JOIN di ON b.instrument_id = di.id) AS backtests,
       (SELECT count(*) FROM ds) AS backfill_symbols,
       (SELECT count(*) FROM bf_watchlist_items w JOIN ds ON w.symbol_id = ds.id) AS watchlist_items,
       (SELECT count(DISTINCT w.watchlist_id) FROM bf_watchlist_items w JOIN ds ON w.symbol_id = ds.id) AS watchlists_with_delta;
SELECT (SELECT count(*) FROM broker_accounts a JOIN brokers b ON b.id = a.broker_id WHERE b.code = 'delta_exchange') AS delta_broker_accounts,
       (SELECT count(*) FROM broker_connections c JOIN broker_accounts a ON a.id = c.broker_account_id JOIN brokers b ON b.id = a.broker_id
         WHERE b.code = 'delta_exchange' AND c.status = 'connected') AS delta_connected,
       (SELECT count(*) FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id WHERE s.source = 'delta') AS delta_backfill_bars,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.exchange = 'DELTA' OR i.data_source = 'delta_exchange') AS delta_chart_candles;

\echo
\echo '== M. Database and table sizes'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows_estimate
FROM pg_stat_user_tables
WHERE relname IN ('bf_ohlcv_bars', 'bf_backfill_jobs', 'bf_symbols', 'ohlcv_candles', 'instruments', 'pcr_snapshots', 'pcr_strike_oi')
ORDER BY pg_total_relation_size(relid) DESC;
