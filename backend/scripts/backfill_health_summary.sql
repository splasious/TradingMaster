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
\echo '== OW. Who owns what: users numbered by sign-up order (no names or emails), counts only; saved_fly_v6 = the account that saved FLY OI SCN v6'
WITH u AS (
  SELECT u.id, row_number() OVER (ORDER BY u.created_at, u.id) AS user_no, u.is_active,
         EXISTS (SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id AND r.name = 'administrator') AS is_admin
  FROM users u
), fly AS (
  SELECT DISTINCT strategy_id FROM strategy_versions
  WHERE python_code LIKE '%FLY OI SCN%' OR python_code LIKE '%F&O Opening-Candle Momentum Scanner%'
)
SELECT u.user_no, u.is_admin, u.is_active,
       EXISTS (SELECT 1 FROM strategy_versions sv JOIN fly ON fly.strategy_id = sv.strategy_id
               WHERE sv.created_by = u.id AND sv.python_code LIKE '%VERSION = 6%') AS saved_fly_v6,
       (SELECT count(*) FROM strategies s WHERE s.owner_id = u.id) AS strategies,
       (SELECT count(*) FROM strategies s JOIN fly ON fly.strategy_id = s.id WHERE s.owner_id = u.id) AS fly_strategies,
       (SELECT count(*) FROM paper_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id WHERE p.user_id = u.id) AS paper_deployments,
       (SELECT count(*) FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id WHERE p.user_id = u.id) AS native_deployments,
       (SELECT count(*) FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id
         JOIN fly ON fly.strategy_id = d.strategy_id WHERE p.user_id = u.id AND d.status = 'active') AS fly_active,
       (SELECT count(*) FROM live_deployments l WHERE l.owner_id = u.id) AS live_deployments,
       (SELECT count(*) FROM bf_watchlists w WHERE w.owner_id = u.id) AS watchlists,
       (SELECT count(*) FROM bf_watchlists w WHERE w.owner_id = u.id AND w.name LIKE 'NSE Nifty%') AS nse_index_watchlists,
       (SELECT count(*) FROM broker_accounts a WHERE a.user_id = u.id) AS broker_accounts
FROM u ORDER BY u.user_no;
SELECT (SELECT count(*) FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id
         JOIN strategies s ON s.id = d.strategy_id WHERE s.owner_id <> p.user_id) AS native_on_others_strategy,
       (SELECT count(*) FROM paper_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id
         JOIN strategies s ON s.id = d.strategy_id WHERE s.owner_id <> p.user_id) AS paper_on_others_strategy,
       (SELECT count(*) FROM live_deployments l JOIN strategies s ON s.id = l.strategy_id WHERE s.owner_id <> l.owner_id) AS live_on_others_strategy,
       (SELECT count(*) FROM live_deployments l JOIN broker_accounts a ON a.id = l.broker_account_id WHERE a.user_id <> l.owner_id) AS live_on_others_broker;

\echo
\echo '== Q2. Open option legs of active advanced deployments (index options only): the price a strategy could read at entry vs the real 5m candle (entry price itself not printed)'
WITH legs AS (
  SELECT (d.state::jsonb->'position'->>'opened_at')::timestamptz AS opened_at, l.key AS leg,
         (l.value->>'instrument_id')::uuid AS instrument_id, (l.value->>'entry_price')::numeric AS entry_price
  FROM paper_native_deployments d
  CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(d.state::jsonb->'position'->'legs') = 'object'
                                     THEN d.state::jsonb->'position'->'legs' ELSE '{}'::jsonb END) l
  WHERE d.status = 'active'
)
SELECT i.symbol, legs.leg, to_char(legs.opened_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS opened_ist,
       pre.timeframe AS last_stored_tf, to_char(pre.ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_stored_candle,
       round(pre.close::numeric, 2) AS last_stored_close, pre.source AS last_stored_source,
       to_char(m.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS m5_at_entry, round(m.low::numeric, 2) AS m5_low, round(m.high::numeric, 2) AS m5_high,
       legs.entry_price BETWEEN m.low::numeric * 0.98 AND m.high::numeric * 1.02 AS entry_in_real_range,
       legs.entry_price = round(pre.close::numeric, 2) AS entry_equals_last_stored_close
FROM legs JOIN instruments i ON i.id = legs.instrument_id
LEFT JOIN LATERAL (SELECT c.timeframe, c.ts, c.close, c.source FROM ohlcv_candles c
                   WHERE c.instrument_id = legs.instrument_id AND c.created_at <= legs.opened_at
                   ORDER BY c.ts DESC LIMIT 1) pre ON true
LEFT JOIN LATERAL (SELECT c.ts, c.low, c.high FROM ohlcv_candles c
                   WHERE c.instrument_id = legs.instrument_id AND c.timeframe = '5m' AND c.ts <= legs.opened_at
                   ORDER BY c.ts DESC LIMIT 1) m ON true
WHERE i.symbol LIKE 'NIFTY%' OR i.symbol LIKE 'BANKNIFTY%'
ORDER BY 1;
\echo '-- NIFTY option candles saved today (IST) 09:15-10:15 per 5-min saving window and source (did live data flow at entry time?)'
SELECT to_char(date_trunc('hour', c.created_at AT TIME ZONE 'Asia/Kolkata')
               + floor(extract(minute FROM c.created_at AT TIME ZONE 'Asia/Kolkata') / 5) * interval '5 min', 'HH24:MI') AS saved_window,
       c.source, count(*) AS candles
FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
WHERE i.exchange = 'NFO' AND i.symbol LIKE 'NIFTY%'
  AND (c.created_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
  AND (c.created_at AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '10:15'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== Q3. Advanced trades opened today (IST), per leg: the real 5m candle open at the entry minute, in the chart table and the backfill copy; does the recorded entry match it (entry price not printed)'
WITH legs AS (
  SELECT t.opened_at, t.closed_at, t.exit_reason, l.value->>'side' AS side,
         (l.value->>'instrument_id')::uuid AS instrument_id, (l.value->>'entry_price')::numeric AS entry_price
  FROM paper_native_trades t CROSS JOIN LATERAL jsonb_array_elements(t.legs::jsonb) l
  WHERE (t.opened_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
), bucket AS (
  SELECT legs.*, i.symbol,
         date_trunc('hour', legs.opened_at) + floor(extract(minute FROM legs.opened_at) / 5) * interval '5 min' AS m5_ts
  FROM legs JOIN instruments i ON i.id = legs.instrument_id
  WHERE i.symbol LIKE 'NIFTY%' OR i.symbol LIKE 'BANKNIFTY%'
)
SELECT b.symbol, b.side, to_char(b.opened_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS opened_ist,
       to_char(b.closed_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS closed_ist, b.exit_reason,
       to_char(b.m5_ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS m5_candle,
       (SELECT round(c.open::numeric, 2) FROM ohlcv_candles c WHERE c.instrument_id = b.instrument_id AND c.timeframe = '5m' AND c.ts = b.m5_ts) AS chart_open,
       (SELECT round(x.open::numeric, 2) FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
         WHERE s.source = 'zerodha_nfo' AND s.symbol = b.symbol AND x.timeframe = '5m' AND x.ts = b.m5_ts) AS backfill_open,
       abs(b.entry_price - coalesce(
         (SELECT c.open::numeric FROM ohlcv_candles c WHERE c.instrument_id = b.instrument_id AND c.timeframe = '5m' AND c.ts = b.m5_ts),
         (SELECT x.open::numeric FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
           WHERE s.source = 'zerodha_nfo' AND s.symbol = b.symbol AND x.timeframe = '5m' AND x.ts = b.m5_ts))) < 0.01 AS entry_matches_real
FROM bucket b ORDER BY b.opened_at, b.symbol;

\echo
\echo '== Q3b. The same legs vs the real 15m candle -- NIFTY weekly options are backfilled at 15m only; an entry in the first 5 minutes of a 15m candle has that candle''s open as its 5m open (entry price not printed)'
WITH legs AS (
  SELECT t.opened_at, l.value->>'side' AS side,
         CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id,
         CASE WHEN l.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'entry_price')::numeric END AS entry_price
  FROM paper_native_trades t CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE jsonb_typeof(l.value) = 'object' AND (t.opened_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
), b AS (
  SELECT legs.*, i.symbol, date_trunc('hour', legs.opened_at) + floor(extract(minute FROM legs.opened_at) / 15) * interval '15 min' AS m15_ts
  FROM legs JOIN instruments i ON i.id = legs.instrument_id
  WHERE i.symbol LIKE 'NIFTY%' OR i.symbol LIKE 'BANKNIFTY%'
), r AS (
  SELECT b.*, c.open::numeric AS o, c.low::numeric AS lo, c.high::numeric AS hi,
         (SELECT x.open::numeric FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
           WHERE s.source = 'zerodha_nfo' AND s.symbol = b.symbol AND x.timeframe = '15m' AND x.ts = b.m15_ts) AS bf_open
  FROM b LEFT JOIN ohlcv_candles c ON c.instrument_id = b.instrument_id AND c.timeframe = '15m' AND c.ts = b.m15_ts
)
SELECT symbol, side, to_char(opened_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS opened_ist,
       to_char(m15_ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS m15_candle, opened_at - m15_ts < interval '5 min' AS in_first_5_min,
       round(o, 2) AS chart_open, round(lo, 2) AS chart_low, round(hi, 2) AS chart_high, round(bf_open, 2) AS backfill_open,
       round((entry_price - o) / nullif(o, 0) * 100, 2) AS entry_pct_off_open, entry_price BETWEEN lo AND hi AS entry_in_15m_range
FROM r ORDER BY opened_at, symbol;

\echo
\echo '== Q6. AM OP TRD 15 MIN closed trades, per leg: entry and exit vs the real 15m candle at that minute (no prices)'
WITH legs AS (
  SELECT t.opened_at, t.closed_at, l.value->>'side' AS side,
         CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id,
         CASE WHEN l.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'entry_price')::numeric END AS entry_price,
         CASE WHEN l.value->>'exit_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'exit_price')::numeric END AS exit_price
  FROM paper_native_trades t
  JOIN paper_native_deployments d ON d.id = t.deployment_id JOIN strategies s ON s.id = d.strategy_id
  CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE trim(s.name) = 'AM OP TRD 15 MIN' AND jsonb_typeof(l.value) = 'object'
), m AS (
  SELECT legs.*,
         date_trunc('hour', legs.opened_at) + floor(extract(minute FROM legs.opened_at) / 15) * interval '15 min' AS e_ts,
         date_trunc('hour', legs.closed_at) + floor(extract(minute FROM legs.closed_at) / 15) * interval '15 min' AS x_ts
  FROM legs
), r AS (
  SELECT m.*, e.open::numeric AS e_open, e.low::numeric AS e_low, e.high::numeric AS e_high,
         x.open::numeric AS x_open, x.low::numeric AS x_low, x.high::numeric AS x_high
  FROM m
  LEFT JOIN ohlcv_candles e ON e.instrument_id = m.instrument_id AND e.timeframe = '15m' AND e.ts = m.e_ts
  LEFT JOIN ohlcv_candles x ON x.instrument_id = m.instrument_id AND x.timeframe = '15m' AND x.ts = m.x_ts
)
SELECT dense_rank() OVER (ORDER BY opened_at) AS trade_no, side,
       to_char(opened_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS bought_ist, opened_at - e_ts < interval '5 min' AS entry_in_first_5_min,
       round((entry_price - e_open) / nullif(e_open, 0) * 100, 2) AS entry_pct_off_15m_open, entry_price BETWEEN e_low AND e_high AS entry_in_15m_range,
       to_char(closed_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS closed_ist,
       round((exit_price - x_open) / nullif(x_open, 0) * 100, 2) AS exit_pct_off_15m_open, exit_price BETWEEN x_low AND x_high AS exit_in_15m_range
FROM r ORDER BY opened_at, side;

\echo
\echo '== AM1. AM OP TRD 15 MIN deployments: status, last run, what the last run said (its NIFTY-only messages; an error shows its type only), any open position, its saved code (repo file md5 6386eaee813268e9839408a14fafccb6)'
SELECT d.status, to_char(d.last_evaluated_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS last_run_ist,
       round(extract(epoch FROM now() - d.last_evaluated_at)) AS secs_ago, d.last_signal,
       CASE WHEN d.last_signal = 'ERROR' THEN split_part(d.last_signal_reason, ':', 1) ELSE d.last_signal_reason END AS last_reason,
       d.state::jsonb -> 'position' ->> 'regime' AS open_regime,
       to_char((d.state::jsonb -> 'position' ->> 'opened_at')::timestamptz AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS open_since_ist,
       md5(replace(v.python_code, E'\r', '')) = '6386eaee813268e9839408a14fafccb6' AS same_as_repo,
       (regexp_match(v.python_code, 'ENTRY_TIME = dtime\(([0-9, ]+)\)'))[1] AS entry_time,
       (regexp_match(v.python_code, 'SIDEWAYS_LOW = ([0-9.]+)'))[1] || '-' || (regexp_match(v.python_code, 'SIDEWAYS_HIGH = ([0-9.]+)'))[1] AS sideways,
       (regexp_match(v.python_code, 'BEARISH_ENTRY = ([0-9.]+)'))[1] AS bearish_below, (regexp_match(v.python_code, 'BULLISH_ENTRY = ([0-9.]+)'))[1] AS bullish_above
FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id JOIN strategy_versions v ON v.id = d.strategy_version_id
WHERE trim(s.name) = 'AM OP TRD 15 MIN' ORDER BY d.created_at;

SELECT count(*) FILTER (WHERE status = 'active') AS active_native_deployments,
       to_char(max(last_evaluated_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS latest_native_run_ist,
       (SELECT count(*) FROM instruments WHERE symbol = 'NIFTY 50') AS nifty_50_instruments
FROM paper_native_deployments;

\echo
\echo '== AM2. AM OP TRD 15 MIN legs opened/closed per day (audit log, last 3 days IST)'
SELECT (a.created_at AT TIME ZONE 'Asia/Kolkata')::date AS day_ist, a.action, count(*) AS events,
       to_char(min(a.created_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS first_ist,
       to_char(max(a.created_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS last_ist
FROM audit_logs a JOIN paper_native_deployments d ON a.object_id = d.id::text JOIN strategies s ON s.id = d.strategy_id
WHERE trim(s.name) = 'AM OP TRD 15 MIN' AND a.object_type = 'paper_native_deployment' AND a.created_at > now() - interval '3 days'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== AM3. Today''s PCR records as AM OP TRD reads them (the latest record at/before each run with 90% of contracts priced): regime by its rules -- sideways 0.80-1.20, bullish above 1.25, bearish below 0.75, otherwise buffer (no entry)'
SELECT to_char(ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS mark_ist, to_char(captured_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS saved_ist, source,
       contracts_with_oi || '/' || contracts_expected AS coverage, contracts_expected > 0 AND contracts_with_oi >= 0.9 * contracts_expected AS usable,
       round(spot::numeric, 1) AS nifty, round(pcr::numeric, 3) AS pcr,
       CASE WHEN pcr IS NULL THEN 'none' WHEN pcr BETWEEN 0.80 AND 1.20 THEN 'sideways' WHEN pcr > 1.25 THEN 'bullish'
            WHEN pcr < 0.75 THEN 'bearish' ELSE 'buffer' END AS regime
FROM pcr_snapshots
WHERE underlying = 'NIFTY' AND session_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
ORDER BY ts;

\echo
\echo '== AM4. NIFTY option contracts it can pick: the next 2 expiries, strikes within 600 of the latest PCR spot'
WITH u AS (SELECT id FROM instruments WHERE symbol = 'NIFTY 50'),
e AS (
  SELECT DISTINCT expiry FROM instruments
  WHERE underlying_instrument_id IN (SELECT id FROM u) AND instrument_type = 'option' AND expiry >= (now() AT TIME ZONE 'Asia/Kolkata')::date
  ORDER BY expiry LIMIT 2)
SELECT i.expiry, i.data_source, count(*) FILTER (WHERE i.option_type = 'CE') AS ce, count(*) FILTER (WHERE i.option_type = 'PE') AS pe,
       count(*) FILTER (WHERE i.lot_size IS NULL) AS no_lot_size, min(i.strike) AS min_strike, max(i.strike) AS max_strike
FROM instruments i JOIN e ON e.expiry = i.expiry
WHERE i.underlying_instrument_id IN (SELECT id FROM u) AND i.instrument_type = 'option'
  AND abs(i.strike - (SELECT spot FROM pcr_snapshots WHERE underlying = 'NIFTY' ORDER BY ts DESC LIMIT 1)) <= 600
GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== AM5. Zerodha login: connect/disconnect events (last 2 days IST) and when the saved access token last changed (times only)'
SELECT to_char(a.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS at_ist, a.action
FROM audit_logs a WHERE a.action IN ('BROKER_CONNECTED', 'BROKER_DISCONNECTED') AND a.created_at > now() - interval '2 days'
ORDER BY a.created_at;

SELECT c.status, to_char(cr.updated_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS token_saved_ist,
       to_char(c.last_heartbeat_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS last_heartbeat_ist
FROM broker_accounts ba JOIN brokers b ON b.id = ba.broker_id
LEFT JOIN broker_connections c ON c.broker_account_id = ba.id LEFT JOIN broker_credentials cr ON cr.broker_account_id = ba.id
WHERE b.code = 'zerodha_kite';

\echo
\echo '== MR0. MACD - RSI - 15 MIN: saved code versions and their deployments (fresh-cross version md5 6a2b57d65304c397ea35bd56dcdee0ce; the one before, 3e4a8b5ac9c85c20683a74159d793f86)'
SELECT sv.version_number, to_char(sv.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS saved_ist,
       md5(replace(sv.python_code, E'\r', '')) = '6a2b57d65304c397ea35bd56dcdee0ce' AS fresh_cross_version, md5(replace(sv.python_code, E'\r', '')) = '3e4a8b5ac9c85c20683a74159d793f86' AS version_before,
       sv.python_code LIKE '%buy = (macd.shift() < 0) & (macd > 0)%' AS macd_line_rule,
       (regexp_match(sv.python_code, 'FAST, SLOW = ([0-9]+, [0-9]+)'))[1] AS fast_slow,
       (regexp_match(sv.python_code, 'MIN_BARS = ([0-9]+)'))[1] AS min_bars, (regexp_match(sv.python_code, 'HISTORY_BARS = ([0-9]+)'))[1] AS history_bars,
       d.status, to_char(d.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS deployed_ist,
       to_char(d.stopped_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS stopped_ist, d.state::jsonb ->> 'seeded' AS seeded,
       (SELECT count(*) FROM jsonb_object_keys(CASE WHEN jsonb_typeof(d.state::jsonb -> 'holdings') = 'object' THEN d.state::jsonb -> 'holdings' ELSE '{}'::jsonb END)) AS holding_now
FROM strategies s JOIN strategy_versions sv ON sv.strategy_id = s.id LEFT JOIN paper_native_deployments d ON d.strategy_version_id = sv.id
WHERE s.name ILIKE 'MACD%RSI%15%MIN%'
ORDER BY sv.created_at, d.created_at;

\echo
\echo '== MR1/MR2. MACD - RSI - 15 MIN: MACD(12,26) line recomputed as the strategy does (last 300 finished 15m candles, EMAs seeded at the first) at every buy and sell -- from the chart candles it could see then (saved by that moment) and from Kite''s final candles (backfill copy). No stock names or prices.'
\echo '   buy rule until the fresh-cross version: the latest zero-cross is up (MACD went <0 to >0 and hasn''t crossed down since), 151+ candles; the first fill after deploying (seed) skips it by design'
\echo '   fresh_cross (the rule from the fresh-cross version on): that up-cross is on the newest candle seen, and the buy came before the next candle closed'
\echo '   sell rule: a down-cross (>0 to <0) on a candle that closed after the buy, and the latest cross is down'
WITH RECURSIVE ev AS (
  SELECT t.deployment_id, t.opened_at AS entry_at, t.closed_at AS exit_at, 'closed' AS kind,
         CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id
  FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id JOIN strategies s ON s.id = d.strategy_id
  CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE s.name ILIKE 'MACD%RSI%15%MIN%' AND jsonb_typeof(l.value) = 'object'
  UNION ALL
  SELECT d.id, CASE WHEN h.value->>'opened_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}' THEN (h.value->>'opened_at')::timestamptz END, NULL, 'open',
         CASE WHEN h.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (h.value->>'instrument_id')::uuid END
  FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id
  CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(d.state::jsonb->'holdings') = 'object' THEN d.state::jsonb->'holdings' ELSE '{}'::jsonb END) h
  WHERE s.name ILIKE 'MACD%RSI%15%MIN%' AND d.status = 'active' AND jsonb_typeof(h.value) = 'object'
), e AS (
  SELECT row_number() OVER (ORDER BY ev.entry_at, ev.instrument_id) AS n, ev.*, i.symbol,
         ev.entry_at < min(ev.entry_at) OVER (PARTITION BY ev.deployment_id) + interval '1 minute' AS seed
  FROM ev JOIN instruments i ON i.id = ev.instrument_id
  WHERE ev.entry_at IS NOT NULL
), pt AS (
  SELECT n, 'buy' AS what, entry_at AS at FROM e
  UNION ALL
  SELECT n, 'sell', exit_at FROM e WHERE exit_at IS NOT NULL
), bars AS (
  SELECT pt.n, pt.what, b.src, b.ts, b.close, row_number() OVER (PARTITION BY pt.n, pt.what, b.src ORDER BY b.ts) AS rn
  FROM pt JOIN e ON e.n = pt.n
  CROSS JOIN LATERAL (
    (SELECT 'chart' AS src, c.ts, c.close::float8 AS close FROM ohlcv_candles c
      WHERE c.instrument_id = e.instrument_id AND c.timeframe = '15m' AND c.ts + interval '15 min' <= pt.at AND c.created_at <= pt.at
      ORDER BY c.ts DESC LIMIT 300)
    UNION ALL
    (SELECT 'kite', x.ts, x.close::float8 FROM bf_ohlcv_bars x JOIN bf_symbols bs ON bs.id = x.symbol_id
      WHERE bs.source = 'zerodha' AND bs.symbol = e.symbol AND x.timeframe = '15m' AND x.ts + interval '15 min' <= pt.at
      ORDER BY x.ts DESC LIMIT 300)
  ) b
), ema AS (
  SELECT n, what, src, rn, ts, close, close AS e12, close AS e26 FROM bars WHERE rn = 1
  UNION ALL
  SELECT b.n, b.what, b.src, b.rn, b.ts, b.close,
         ema.e12 + (b.close - ema.e12) * (2.0::float8 / 13), ema.e26 + (b.close - ema.e26) * (2.0::float8 / 27)
  FROM ema JOIN bars b ON b.n = ema.n AND b.what = ema.what AND b.src = ema.src AND b.rn = ema.rn + 1
), m AS (
  SELECT n, what, src, rn, ts, close, CASE WHEN rn >= 26 THEN e12 - e26 END AS macd FROM ema
), x AS (
  SELECT m.*, CASE WHEN lag(macd) OVER w < 0 AND macd > 0 THEN 'up' WHEN lag(macd) OVER w > 0 AND macd < 0 THEN 'down' END AS xing
  FROM m WINDOW w AS (PARTITION BY n, what, src ORDER BY rn)
), st AS (
  SELECT n, what, src, max(rn) AS bars,
         (array_agg(macd / nullif(close, 0) * 100 ORDER BY rn DESC))[1] AS macd_pct,
         (array_agg(xing ORDER BY rn DESC) FILTER (WHERE xing IS NOT NULL))[1] AS last_cross,
         (array_agg(ts ORDER BY rn DESC) FILTER (WHERE xing IS NOT NULL))[1] AS last_cross_ts,
         max(rn) - (array_agg(rn ORDER BY rn DESC) FILTER (WHERE xing IS NOT NULL))[1] AS candles_since,
         (array_agg(ts ORDER BY rn DESC) FILTER (WHERE xing = 'down' AND rn >= 151))[1] + interval '15 min' AS last_sell_at,
         (array_agg(ts ORDER BY rn DESC))[1] AS last_candle_ts
  FROM x GROUP BY n, what, src
), v AS (
  SELECT e.n, e.kind, e.seed, e.entry_at, e.exit_at, p.what, p.at,
         ch.bars AS ch_bars, ch.last_candle_ts AS ch_last, ch.macd_pct AS ch_macd, ch.last_cross AS ch_cross, ch.last_cross_ts AS ch_cross_ts, ch.candles_since AS ch_since, ch.last_sell_at AS ch_sell_at,
         k.bars AS k_bars, k.macd_pct AS k_macd, k.last_cross AS k_cross, k.last_cross_ts AS k_cross_ts, k.candles_since AS k_since, k.last_sell_at AS k_sell_at
  FROM pt p JOIN e ON e.n = p.n
  LEFT JOIN st ch ON ch.n = p.n AND ch.what = p.what AND ch.src = 'chart'
  LEFT JOIN st k ON k.n = p.n AND k.what = p.what AND k.src = 'kite'
)
SELECT n AS trade, kind, what, to_char(at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS at_ist, seed,
       to_char(ch_last AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_candle_seen, ch_bars AS candles,
       round(ch_macd::numeric, 3) AS macd_pct, ch_cross AS last_cross, to_char(ch_cross_ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS cross_candle_ist, ch_since AS candles_ago,
       CASE WHEN what = 'buy' THEN seed OR (ch_bars >= 151 AND ch_cross = 'up')
            ELSE ch_cross = 'down' AND ch_sell_at > entry_at END AS rule_ok,
       CASE WHEN what = 'buy' THEN ch_bars >= 151 AND ch_cross = 'up' AND ch_since = 0 AND at < ch_last + interval '30 min' END AS fresh_cross,
       round(k_macd::numeric, 3) AS kite_macd_pct, k_cross AS kite_last_cross, to_char(k_cross_ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS kite_cross_ist,
       CASE WHEN what = 'buy' THEN seed OR (k_bars >= 151 AND k_cross = 'up')
            ELSE k_cross = 'down' AND k_sell_at > entry_at END AS kite_rule_ok,
       CASE WHEN what = 'sell' AND ch_sell_at IS NOT NULL THEN round(extract(epoch FROM at - ch_sell_at) / 60) END AS sell_min_after_cross
FROM v ORDER BY n, what;

\echo
\echo '== HG1. A renamed NSE stock (old symbol, its -BE form, new symbol): backfill symbols, bars, nightly coverage, watchlists, recent jobs (labels only)'
WITH names(label, symbol) AS (VALUES ('old', 'HEG'), ('old -BE', 'HEG-BE'), ('new', 'HEGAM'))
SELECT n.label, s.source, (SELECT count(*) FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id) AS bars,
       (SELECT to_char(max(b.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id) AS last_bar,
       (SELECT count(*) FROM bf_coverage c WHERE c.symbol_id = s.id) AS coverage_rows,
       (SELECT count(*) FROM bf_watchlist_items w WHERE w.symbol_id = s.id) AS in_watchlists,
       (SELECT count(*) FROM bf_backfill_jobs j WHERE j.symbol_id = s.id AND j.status = 'failed' AND j.created_at > now() - interval '3 days') AS failed_jobs_3d,
       (SELECT count(*) FROM bf_backfill_jobs j WHERE j.symbol_id = s.id AND j.status = 'completed' AND j.created_at > now() - interval '3 days') AS done_jobs_3d,
       to_char(s.last_synced_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS copied_to_charts
FROM names n JOIN bf_symbols s ON s.symbol = n.symbol ORDER BY n.label, s.source;

\echo
\echo '== HG2. The same in the chart catalog and the strategies (labels only)'
WITH names(label, symbol) AS (VALUES ('old', 'HEG'), ('old -BE', 'HEG-BE'), ('new', 'HEGAM'))
SELECT n.label, i.exchange, i.instrument_type, i.is_active,
       (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = i.id) AS candles,
       (SELECT to_char(max(c.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') FROM ohlcv_candles c WHERE c.instrument_id = i.id) AS last_candle,
       (SELECT count(*) FROM paper_native_deployments d
         WHERE d.status = 'active' AND (d.state::text LIKE '%' || i.id::text || '%')) AS active_deployments_holding_it,
       (SELECT count(*) FROM paper_deployments pd WHERE pd.instrument_id = i.id AND pd.status = 'active') AS simple_deployments_on_it
FROM names n JOIN instruments i ON i.symbol = n.symbol ORDER BY n.label, i.exchange;

SELECT s.name AS strategy, count(DISTINCT sv.id) AS versions_listing_old_symbol,
       max(sv.version_number) FILTER (WHERE sv.python_code ~ '[''"]HEG[''"]') AS latest_version_listing_it,
       (SELECT max(v2.version_number) FROM strategy_versions v2 WHERE v2.strategy_id = s.id) AS latest_version,
       (SELECT count(*) FROM paper_native_deployments d WHERE d.strategy_id = s.id AND d.status = 'active') AS active_deployments
FROM strategies s JOIN strategy_versions sv ON sv.strategy_id = s.id
WHERE sv.python_code ~ '[''"]HEG[''"]'
GROUP BY s.id, s.name ORDER BY 1;

\echo
\echo '== HG3. Old symbol''s last daily closes as a ratio of the new symbol''s price today (~238.25, the middle of its 20% circuit band): ~1 = same price level, ~2/~5 = a split or bonus came with the rename'
SELECT to_char(b.ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') AS day, round((b.close / 238.25)::numeric, 3) AS close_vs_new_price
FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
WHERE s.source = 'zerodha' AND s.symbol = 'HEG' AND b.timeframe = '1d'
ORDER BY b.ts DESC LIMIT 5;

\echo
\echo '== NS1. The 7 stocks added to MACD - RSI - 15 MIN and RS Rotation 15 MIN on 1 Oct (numbered, no names): chart instrument, backfill symbol, stored 15m/5m candles, 15m candles on 30 Sep, the 9 RS seed slots of 30 Sep (13:15-15:15), last 15m candle'
WITH names(n, symbol) AS (VALUES (1, 'CUPID'), (2, 'HFCL'), (3, 'KIRLOSENG'), (4, 'MTARTECH'), (5, 'STLTECH'), (6, 'TDPOWERSYS'), (7, 'WELCORP'))
SELECT n.n,
       (SELECT count(*) FROM instruments i WHERE i.exchange = 'NSE' AND i.symbol = n.symbol) AS nse_instruments,
       (SELECT count(*) FROM bf_symbols s WHERE s.source = 'zerodha' AND s.symbol = n.symbol) AS bf_symbols,
       (SELECT count(*) FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id WHERE s.source = 'zerodha' AND s.symbol = n.symbol AND b.timeframe = '15m') AS bf_15m,
       (SELECT count(*) FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id WHERE s.source = 'zerodha' AND s.symbol = n.symbol AND b.timeframe = '5m') AS bf_5m,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.exchange = 'NSE' AND i.symbol = n.symbol AND c.timeframe = '15m') AS chart_15m,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.exchange = 'NSE' AND i.symbol = n.symbol AND c.timeframe = '5m') AS chart_5m,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.exchange = 'NSE' AND i.symbol = n.symbol AND c.timeframe = '15m'
          AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-09-30') AS chart_15m_30sep,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.exchange = 'NSE' AND i.symbol = n.symbol AND c.timeframe = '15m'
          AND c.ts BETWEEN TIMESTAMPTZ '2026-09-30 13:15+05:30' AND TIMESTAMPTZ '2026-09-30 15:15+05:30') AS rs_seed_slots,
       (SELECT to_char(max(c.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
          WHERE i.exchange = 'NSE' AND i.symbol = n.symbol AND c.timeframe = '15m') AS last_15m
FROM names n ORDER BY n.n;

\echo
\echo '== NS2. The 50 stocks already on both lists, as counts: how many have >= 151 stored 15m candles (MACD warm-up) and all 9 RS seed slots of 30 Sep; NIFTY 50 seed slots'
WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL')),
per AS (
  SELECT f.symbol,
         (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '15m') AS c15,
         (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '15m'
            AND c.ts BETWEEN TIMESTAMPTZ '2026-09-30 13:15+05:30' AND TIMESTAMPTZ '2026-09-30 15:15+05:30') AS seed
  FROM fifty f LEFT JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol
)
SELECT count(*) AS stocks, count(*) FILTER (WHERE c15 >= 151) AS with_151_15m, count(*) FILTER (WHERE seed = 9) AS with_9_seed_slots,
       (SELECT count(*) FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id WHERE i.symbol = 'NIFTY 50' AND c.timeframe = '15m'
          AND c.ts BETWEEN TIMESTAMPTZ '2026-09-30 13:15+05:30' AND TIMESTAMPTZ '2026-09-30 15:15+05:30') AS nifty_seed_slots
FROM per;

\echo
\echo '== NS3. The same 50: 30 Sep 15m chart candles per slot (IST) -- stocks having it, by source -- and the backfill copy (bf 15m) for the same slot'
WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL'))
SELECT to_char(c.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS slot_ist, c.source, count(DISTINCT c.instrument_id) AS stocks
FROM fifty f JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol
JOIN ohlcv_candles c ON c.instrument_id = i.id AND c.timeframe = '15m' AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-09-30'
GROUP BY 1, 2 ORDER BY 1, 2;

WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL'))
SELECT (SELECT count(*) FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol) AS with_bf_symbol,
       (SELECT count(DISTINCT s.id) FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol
          JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = '15m' AND (b.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-09-30') AS bf_15m_on_30sep,
       (SELECT count(DISTINCT s.id) FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol
          JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = '5m' AND (b.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-09-30') AS bf_5m_on_30sep,
       (SELECT to_char(max(b.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol
          JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = '15m') AS bf_15m_last,
       (SELECT to_char(min(s.last_synced_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol) AS oldest_copy_ist,
       (SELECT to_char(max(s.last_synced_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol) AS newest_copy_ist;

\echo
\echo '== NS4. The day''s last candles, last 6 sessions, the 50 listed stocks: how many have the 15:15 15m and 15:25 5m candle -- chart table and backfill copy'
WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL')),
days AS (SELECT DISTINCT (c.ts AT TIME ZONE 'Asia/Kolkata')::date AS d FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
         WHERE i.symbol = 'NIFTY 50' AND c.timeframe = '15m' AND c.ts > now() - interval '12 days' ORDER BY 1 DESC LIMIT 6)
SELECT to_char(d.d, 'DD Mon') AS session,
  (SELECT count(*) FROM fifty f JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol JOIN ohlcv_candles c ON c.instrument_id = i.id
     AND c.timeframe = '15m' AND c.ts = (d.d + time '15:15') AT TIME ZONE 'Asia/Kolkata') AS chart_1515_15m,
  (SELECT count(*) FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol JOIN bf_ohlcv_bars b ON b.symbol_id = s.id
     AND b.timeframe = '15m' AND b.ts = (d.d + time '15:15') AT TIME ZONE 'Asia/Kolkata') AS bf_1515_15m,
  (SELECT count(*) FROM fifty f JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol JOIN ohlcv_candles c ON c.instrument_id = i.id
     AND c.timeframe = '5m' AND c.ts = (d.d + time '15:25') AT TIME ZONE 'Asia/Kolkata') AS chart_1525_5m,
  (SELECT count(*) FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol JOIN bf_ohlcv_bars b ON b.symbol_id = s.id
     AND b.timeframe = '5m' AND b.ts = (d.d + time '15:25') AT TIME ZONE 'Asia/Kolkata') AS bf_1525_5m,
  (SELECT count(*) FROM fifty f JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol JOIN ohlcv_candles c ON c.instrument_id = i.id
     AND c.timeframe = '15m' AND c.ts = (d.d + time '15:00') AT TIME ZONE 'Asia/Kolkata') AS chart_1500_15m
FROM days d ORDER BY d.d DESC;

\echo
\echo '== NS5. MACD - RSI - 15 MIN versions 12 on: is each the 30 Sep version (v12) plus the 7 stocks and nothing else? (comments, whitespace, commas and the 7 names ignored)'
WITH v AS (
  SELECT sv.version_number, sv.python_code AS code, length(sv.python_code) AS chars,
         md5(replace(sv.python_code, E'\r', '')) = '561759bbee3e25378f16c524c7f2f07b' AS is_57_builtin,
         (SELECT count(*) FROM unnest(ARRAY['CUPID','HFCL','KIRLOSENG','MTARTECH','STLTECH','TDPOWERSYS','WELCORP']) n WHERE sv.python_code LIKE '%"' || n || '"%') AS of_7_listed,
         regexp_replace(regexp_replace(regexp_replace(replace(sv.python_code, E'\r', ''), '#[^\n]*', '', 'g'),
           '"(CUPID|HFCL|KIRLOSENG|MTARTECH|STLTECH|TDPOWERSYS|WELCORP)"', '', 'g'), '[\s,]', '', 'g') AS core,
         (SELECT count(*) FROM paper_native_deployments d WHERE d.strategy_version_id = sv.id) AS deployments
  FROM strategies s JOIN strategy_versions sv ON sv.strategy_id = s.id
  WHERE s.name ILIKE 'MACD%RSI%15%MIN%' AND sv.version_number >= 12
)
SELECT version_number, chars, is_57_builtin, of_7_listed, core = (SELECT core FROM v WHERE version_number = 12) AS same_as_v12_otherwise, deployments
FROM v ORDER BY version_number;

\echo
\echo '== NS6. RS Rotation 15 MIN: strategies whose code is the 15-minute RS rotation, their versions and deployments'
SELECT sv.version_number, to_char(sv.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS saved_ist,
       md5(replace(sv.python_code, E'\r', '')) = '5a9c766dbf2905e41410671e435753d5' AS is_57_builtin,
       md5(replace(sv.python_code, E'\r', '')) = '99da3a7283fd0afd59add3360cae2cdd' AS is_50_builtin,
       d.status, to_char(d.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS deployed_ist,
       md5(replace(sv.python_code, E'\r', '')) = '988246769bebae7d1d8c3cdfbcf91be8' AS is_carry_builtin,
       to_char(d.last_evaluated_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS last_run_ist, d.last_signal,
       substring(d.last_signal_reason FROM '^[0-9:]+ close|^holding|^no live|^skipped') AS reason_kind,
       substring(d.last_signal_reason FROM '(ranked [0-9]+/[0-9]+|warming up: [0-9]+/[0-9]+)') AS ranked,
       (regexp_match(d.last_signal_reason, 'bought: ([^|]*)'))[1] = 'none ' AS bought_none,
       array_length(regexp_split_to_array((regexp_match(d.last_signal_reason, 'bought: ([^|]*)'))[1], ','), 1) AS bought_n,
       substring(d.last_signal_reason FROM 'holding [0-9]+/[0-9]+') AS holding,
       jsonb_array_length(COALESCE(d.state::jsonb -> '_series' -> 'bars', '[]'::jsonb)) AS series_bars,
       (SELECT count(*) FROM paper_native_trades t WHERE t.deployment_id = d.id) AS closed_trades,
       (SELECT count(*) FROM jsonb_object_keys(CASE WHEN jsonb_typeof(d.state::jsonb -> 'holdings') = 'object' THEN d.state::jsonb -> 'holdings' ELSE '{}'::jsonb END)) AS holding_now
FROM strategy_versions sv LEFT JOIN paper_native_deployments d ON d.strategy_version_id = sv.id
WHERE sv.python_code LIKE '%RS Rotation 15 MIN -- Relative-Strength Rotation on 15-minute candles%'
ORDER BY sv.created_at, d.created_at;

\echo
\echo '== NS7. Why most have no 15:15 candle: 15m jobs that covered 30 Sep for the 50, split by whether the stock has that candle now -- when they ran (IST), dates, status, bars'
WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL')),
sym AS (
  SELECT s.id, EXISTS (SELECT 1 FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id AND b.timeframe = '15m' AND b.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30') AS has_1515
  FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol
)
SELECT sym.has_1515, to_char(date_trunc('hour', j.started_at AT TIME ZONE 'Asia/Kolkata') + floor(extract(minute FROM j.started_at AT TIME ZONE 'Asia/Kolkata') / 15) * interval '15 min', 'DD Mon HH24:MI') AS started_ist_15min,
       to_char(j.start_date, 'DD Mon') AS from_day, to_char(j.end_date, 'DD Mon') AS to_day, j.status, (j.run_id IS NOT NULL) AS topup,
       count(*) AS jobs, sum(j.downloaded_count) AS downloaded, sum(j.inserted_count) AS inserted
FROM sym JOIN bf_backfill_jobs j ON j.symbol_id = sym.id AND j.timeframe = '15m'
WHERE (j.end_date IS NULL OR j.end_date >= DATE '2026-09-30') AND (j.start_date IS NULL OR j.start_date <= DATE '2026-09-30') AND j.created_at > TIMESTAMPTZ '2026-09-30 00:00+05:30'
GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY 1, 2;

WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL'))
SELECT EXISTS (SELECT 1 FROM bf_ohlcv_bars b WHERE b.symbol_id = s.id AND b.timeframe = '15m' AND b.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30') AS has_1515,
       to_char(c.last_ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS coverage_last_bar_ist, to_char(c.checked_through, 'DD Mon') AS checked_through, count(*) AS stocks
FROM fifty f JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = f.symbol LEFT JOIN bf_coverage c ON c.symbol_id = s.id AND c.timeframe = '15m'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

\echo
\echo '== NS8. Is the missing 15:15 candle the F&O closing session? The 57 listed stocks by: has F&O contracts, has the 30 Sep 15:15 15m candle -- with how many have each 30 Sep 5m candle from 15:05 on'
WITH lst(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL'),
  ('CUPID'), ('HFCL'), ('KIRLOSENG'), ('MTARTECH'), ('STLTECH'), ('TDPOWERSYS'), ('WELCORP')),
st AS (
  SELECT l.symbol, i.id,
         EXISTS (SELECT 1 FROM instruments f WHERE f.exchange = 'NFO' AND f.underlying_instrument_id = i.id)
           OR EXISTS (SELECT 1 FROM bf_symbols b WHERE b.source = 'zerodha_nfo' AND b.underlying_symbol = l.symbol) AS fno,
         EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '15m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30') AS has_1515,
         EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '15m' AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-09-30') AS has_30sep
  FROM lst l JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = l.symbol
)
SELECT fno, has_1515, count(*) AS stocks,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = st.id AND c.timeframe = '5m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:05+05:30')) AS m5_1505,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = st.id AND c.timeframe = '5m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:10+05:30')) AS m5_1510,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = st.id AND c.timeframe = '5m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30')) AS m5_1515,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = st.id AND c.timeframe = '5m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:20+05:30')) AS m5_1520,
  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = st.id AND c.timeframe = '5m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:25+05:30')) AS m5_1525
FROM st WHERE has_30sep GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '-- NS8b. Since when: per session over the stored 15m history, how many of the 50 have a 15:15 candle (first and last 8 sessions, plus any session where the count changes)'
WITH fifty(symbol) AS (VALUES ('ABCAPITAL'), ('ACUTAAS'), ('ADANIENSOL'), ('ADANIPOWER'), ('AMBER'), ('ANANDRATHI'), ('APARINDS'), ('ASHOKLEY'), ('ATHERENERG'), ('AUBANK'),
  ('BANKINDIA'), ('BHARATFORG'), ('BHEL'), ('BSE'), ('CANBK'), ('CUMMINSIND'), ('DELHIVERY'), ('EICHERMOT'), ('FEDERALBNK'), ('FORTIS'),
  ('GLENMARK'), ('GVT&D'), ('HDFCAMC'), ('HINDALCO'), ('HINDCOPPER'), ('IDEA'), ('IIFL'), ('INDIANB'), ('KARURVYSYA'), ('LAURUSLABS'),
  ('LTF'), ('MANAPPURAM'), ('MCX'), ('MFSL'), ('MUTHOOTFIN'), ('NATIONALUM'), ('NAVINFLUOR'), ('NYKAA'), ('PAYTM'), ('POLYCAB'),
  ('POWERINDIA'), ('RADICO'), ('RBLBANK'), ('SAIL'), ('SBIN'), ('SHRIRAMFIN'), ('SOLARINDS'), ('TVSMOTOR'), ('UNIONBANK'), ('VEDL')),
per AS (
  SELECT (c.ts AT TIME ZONE 'Asia/Kolkata')::date AS d,
         count(DISTINCT c.instrument_id) FILTER (WHERE (c.ts AT TIME ZONE 'Asia/Kolkata')::time = time '15:15') AS with_1515,
         count(DISTINCT c.instrument_id) AS with_any
  FROM fifty f JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = f.symbol JOIN ohlcv_candles c ON c.instrument_id = i.id AND c.timeframe = '15m'
  GROUP BY 1
), lagged AS (SELECT d, with_1515, with_any, lag(with_1515) OVER (ORDER BY d) AS prev, row_number() OVER (ORDER BY d) AS rn, count(*) OVER () AS n FROM per)
SELECT to_char(d, 'DD Mon YYYY') AS session, with_1515, with_any FROM lagged
WHERE rn <= 3 OR rn > n - 3 OR with_1515 IS DISTINCT FROM prev ORDER BY d;

\echo
\echo '== NF1. NIFTY futures in the chart catalog: expiry, lot size, linked to NIFTY 50, stored 5m/15m candles, last candle'
SELECT i.expiry, i.lot_size, i.is_active, (i.underlying_instrument_id IS NOT NULL) AS linked_to_underlying,
       (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '5m') AS c5m,
       (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '15m') AS c15m,
       (SELECT to_char(max(c.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM ohlcv_candles c WHERE c.instrument_id = i.id) AS last_candle_ist
FROM instruments i
WHERE i.exchange = 'NFO' AND i.instrument_type = 'future' AND (i.symbol ~ '^NIFTY[0-9]' AND i.symbol LIKE '%FUT') AND i.symbol NOT LIKE 'NIFTYNXT%'
ORDER BY i.expiry;

\echo
\echo '== NF2. NIFTY option expiries listed (from 30 Sep): strikes, calls/puts, with a 15:15 15m candle on 30 Sep'
SELECT o.expiry, count(*) AS contracts, count(*) FILTER (WHERE o.option_type = 'CE') AS calls, count(*) FILTER (WHERE o.option_type = 'PE') AS puts,
       min(o.strike) AS min_strike, max(o.strike) AS max_strike, min(o.lot_size) AS lot_min, max(o.lot_size) AS lot_max,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM ohlcv_candles c WHERE c.instrument_id = o.id AND c.timeframe = '15m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30')) AS with_1515_candle
FROM instruments o JOIN instruments u ON u.id = o.underlying_instrument_id AND u.symbol = 'NIFTY 50'
WHERE o.exchange = 'NFO' AND o.instrument_type = 'option' AND o.expiry >= DATE '2026-09-30'
GROUP BY o.expiry ORDER BY o.expiry LIMIT 8;

\echo
\echo '== NF3. Where an in-the-money NIFTY option costs about 150 (30 Sep 15:15 close): per expiry and side, the nearest ITM strike and the ITM strike whose premium is closest to 150 -- points in the money, premium, time value'
WITH spot AS (
  SELECT c.close AS s FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  WHERE i.symbol = 'NIFTY 50' AND c.timeframe = '15m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30'
), opt AS (
  SELECT o.expiry, o.option_type, o.strike, c.close AS prem
  FROM instruments o JOIN instruments u ON u.id = o.underlying_instrument_id AND u.symbol = 'NIFTY 50'
  JOIN ohlcv_candles c ON c.instrument_id = o.id AND c.timeframe = '15m' AND c.ts = TIMESTAMPTZ '2026-09-30 15:15+05:30'
  WHERE o.exchange = 'NFO' AND o.instrument_type = 'option' AND o.expiry >= DATE '2026-09-30'
), itm AS (
  SELECT opt.expiry, opt.option_type, opt.strike, opt.prem, abs(opt.strike - spot.s) AS pts FROM opt, spot
  WHERE (opt.option_type = 'PE' AND opt.strike > spot.s) OR (opt.option_type = 'CE' AND opt.strike < spot.s)
)
SELECT 'nearest ITM' AS pick, expiry, option_type, round(pts::numeric) AS itm_points, round(prem::numeric, 1) AS premium, round((prem - pts)::numeric, 1) AS time_value
FROM (SELECT DISTINCT ON (expiry, option_type) * FROM itm ORDER BY expiry, option_type, pts) a
UNION ALL
SELECT 'closest to 150', expiry, option_type, round(pts::numeric), round(prem::numeric, 1), round((prem - pts)::numeric, 1)
FROM (SELECT DISTINCT ON (expiry, option_type) * FROM itm WHERE prem > 0 ORDER BY expiry, option_type, abs(prem - 150)) b
ORDER BY 1 DESC, 2, 3;

\echo
\echo '== EX1. Expired NFO contracts still held, by kind (backfill symbols; today = IST): symbols, with a coverage row, 15m/5m/1d bars recorded in coverage, last expiry, how many expired before the last 7/30/90 days'
SELECT CASE WHEN s.option_type IS NULL THEN 'future' WHEN s.underlying_symbol IN ('NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','NIFTYNXT50') THEN 'index option' ELSE 'stock option' END AS kind, (s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date) AS expired,
       count(DISTINCT s.id) AS symbols, count(DISTINCT c.symbol_id) AS with_coverage,
       COALESCE(sum(c.bar_count) FILTER (WHERE c.timeframe = '15m'), 0) AS bars_15m, COALESCE(sum(c.bar_count) FILTER (WHERE c.timeframe = '5m'), 0) AS bars_5m,
       COALESCE(sum(c.bar_count) FILTER (WHERE c.timeframe = '1d'), 0) AS bars_1d,
       to_char(min(s.expiry), 'DD Mon YYYY') AS first_expiry, to_char(max(s.expiry), 'DD Mon YYYY') AS last_expiry,
       count(DISTINCT s.id) FILTER (WHERE s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date - 7) AS expired_over_7d,
       count(DISTINCT s.id) FILTER (WHERE s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date - 30) AS expired_over_30d,
       count(DISTINCT s.id) FILTER (WHERE s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date - 90) AS expired_over_90d
FROM bf_symbols s LEFT JOIN bf_coverage c ON c.symbol_id = s.id
WHERE s.source = 'zerodha_nfo' AND s.expiry IS NOT NULL
GROUP BY 1, 2 ORDER BY 2 DESC, 1;

\echo
\echo '== EX2. The same expired contracts in the chart catalog (instruments): count by kind and whether still flagged active, plus what points at them (counts only)'
SELECT CASE WHEN i.instrument_type = 'future' THEN 'future' WHEN u.symbol IN ('NIFTY 50','NIFTY BANK','NIFTY FIN SERVICE','NIFTY MID SELECT','NIFTY NEXT 50') THEN 'index option' ELSE 'stock option' END AS kind,
       i.is_active, count(*) AS instruments,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM fo_oi_snapshots f WHERE f.instrument_id = i.id)) AS with_oi_snapshots,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM paper_deployments d WHERE d.instrument_id = i.id)) AS paper_deployments,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM live_deployments d WHERE d.instrument_id = i.id)) AS live_deployments,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM backtest_jobs b WHERE b.instrument_id = i.id)) AS backtests,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM paper_native_deployments d WHERE d.state::text LIKE '%' || i.id::text || '%')) AS in_native_state
FROM instruments i LEFT JOIN instruments u ON u.id = i.underlying_instrument_id
WHERE i.exchange = 'NFO' AND i.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date
GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== EX3. Expired NFO contracts referenced from paper trade history (legs saved with the trade) and recorded jobs: counts'
SELECT (SELECT count(DISTINCT i.id) FROM instruments i JOIN paper_native_trades t ON t.legs::text LIKE '%' || i.id::text || '%'
         WHERE i.exchange = 'NFO' AND i.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date) AS expired_contracts_in_trade_history,
       (SELECT count(*) FROM bf_backfill_jobs j JOIN bf_symbols s ON s.id = j.symbol_id
         WHERE s.source = 'zerodha_nfo' AND s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date AND j.status IN ('pending', 'running')) AS open_jobs_for_expired,
       (SELECT count(*) FROM bf_watchlist_items w JOIN bf_symbols s ON s.id = w.symbol_id
         WHERE s.source = 'zerodha_nfo' AND s.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date) AS expired_in_watchlists;

\echo
\echo '== EX4. Table sizes of what holds the contracts and their candles'
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS total_size, n_live_tup AS rows_estimate
FROM pg_stat_user_tables WHERE relname IN ('bf_ohlcv_bars', 'ohlcv_candles', 'bf_symbols', 'bf_coverage', 'instruments', 'fo_oi_snapshots', 'bf_backfill_jobs', 'pcr_strike_oi')
ORDER BY pg_total_relation_size(relid) DESC;

\echo
\echo '== RT1. Expired NFO contracts retired at the nightly backfill (audit log): when, the expiry cutoff and what went'
SELECT to_char(a.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS at_ist, a.new_value::jsonb ->> 'session' AS session, a.new_value::jsonb ->> 'cutoff' AS expiry_cutoff,
       a.new_value::jsonb ->> 'stock_symbols_deleted' AS stock_symbols, a.new_value::jsonb ->> 'backfill_bars_deleted' AS bf_bars,
       a.new_value::jsonb ->> 'stock_instruments_deleted' AS stock_instruments, a.new_value::jsonb ->> 'chart_candles_deleted' AS chart_candles,
       a.new_value::jsonb ->> 'oi_snapshots_deleted' AS oi_snapshots, a.new_value::jsonb ->> 'index_instruments_hidden' AS index_hidden,
       a.new_value::jsonb ->> 'watchlist_items_removed' AS watchlist_items, a.new_value::jsonb ->> 'kept_in_use' AS kept_in_use
FROM audit_logs a WHERE a.action = 'NFO_EXPIRED_RETIRED' ORDER BY a.created_at DESC LIMIT 6;

\echo
\echo '== RT2. Expired NFO contracts still held, by expiry date: stock contracts, index contracts still active, index contracts hidden (the 29 Sep batch is retired on 6 Oct)'
SELECT to_char(i.expiry, 'DD Mon YYYY') AS expiry,
       count(*) FILTER (WHERE i.symbol !~ '^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTYNXT50|NIFTY)[0-9]') AS stock_contracts,
       count(*) FILTER (WHERE i.symbol ~ '^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTYNXT50|NIFTY)[0-9]' AND i.is_active) AS index_active,
       count(*) FILTER (WHERE i.symbol ~ '^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTYNXT50|NIFTY)[0-9]' AND NOT i.is_active) AS index_hidden
FROM instruments i WHERE i.exchange = 'NFO' AND i.expiry < (now() AT TIME ZONE 'Asia/Kolkata')::date
GROUP BY i.expiry ORDER BY i.expiry;

\echo
\echo '== PF1. Nifty PCR Futures Hedge: deployments on its code, the open position (bias, leg kinds and expiries), last check, and closed trades by exit reason'
SELECT s.name AS strategy, sv.version_number, d.status, to_char(d.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS deployed_ist,
       to_char(d.last_evaluated_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS last_run_ist, d.last_signal,
       d.state::jsonb -> 'position' ->> 'bias' AS bias, d.state::jsonb -> 'position' ->> 'pcr_at_entry' AS pcr_at_entry,
       d.state::jsonb -> 'position' -> 'legs' -> 'future' ->> 'expiry' AS future_expiry, d.state::jsonb -> 'position' -> 'legs' -> 'future' ->> 'side' AS future_side,
       d.state::jsonb -> 'position' -> 'legs' -> 'option' ->> 'option_type' AS option_type, d.state::jsonb -> 'position' -> 'legs' -> 'option' ->> 'expiry' AS option_expiry,
       (SELECT string_agg(t.exit_reason || ' x' || t.n, ', ') FROM (SELECT exit_reason, count(*) AS n FROM paper_native_trades WHERE deployment_id = d.id GROUP BY 1) t) AS trades,
       (SELECT round(sum(pnl)::numeric, 0) FROM paper_native_trades WHERE deployment_id = d.id) AS realised_pnl
FROM strategy_versions sv JOIN strategies s ON s.id = sv.strategy_id JOIN paper_native_deployments d ON d.strategy_version_id = sv.id
WHERE sv.python_code LIKE '%Nifty PCR Futures Hedge -- NIFTY futures with a short in-the-money option%'
ORDER BY d.created_at;

\echo
\echo '== NB1. NFO coverage by timeframe and kind, as the Data Backfill page counts it (counts only): expired by its last bar (expiry on or before that bar''s day), past expiry but its last bar earlier (the page counts these as live and behind), still trading; last-bar days'
WITH c AS (
  SELECT cov.timeframe, s.expiry, (cov.last_ts AT TIME ZONE 'Asia/Kolkata')::date AS last_day,
         CASE WHEN s.option_type IN ('CE', 'PE') AND coalesce(s.underlying_symbol, '') IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50') THEN 'index option'
              WHEN s.option_type IN ('CE', 'PE') THEN 'stock option'
              WHEN coalesce(s.underlying_symbol, '') IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50') THEN 'index future'
              ELSE 'stock future' END AS kind
  FROM bf_coverage cov JOIN bf_symbols s ON s.id = cov.symbol_id
  WHERE s.source = 'zerodha_nfo'
), t AS (SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date AS today)
SELECT c.timeframe, c.kind,
       count(*) FILTER (WHERE c.expiry IS NOT NULL AND c.expiry <= c.last_day) AS expired_by_last_bar,
       count(*) FILTER (WHERE c.expiry > c.last_day AND c.expiry < t.today) AS past_expiry_last_bar_earlier,
       count(*) FILTER (WHERE c.expiry IS NULL OR (c.expiry >= t.today AND c.expiry > c.last_day)) AS still_trading,
       to_char(min(c.last_day) FILTER (WHERE c.expiry > c.last_day AND c.expiry < t.today), 'DD Mon') AS oldest_last_bar_past_expiry,
       to_char(min(c.expiry) FILTER (WHERE c.expiry > c.last_day AND c.expiry < t.today), 'DD Mon') AS first_expiry_past,
       to_char(max(c.expiry) FILTER (WHERE c.expiry > c.last_day AND c.expiry < t.today), 'DD Mon') AS last_expiry_past,
       to_char(min(c.last_day) FILTER (WHERE c.expiry IS NULL OR (c.expiry >= t.today AND c.expiry > c.last_day)), 'DD Mon') AS oldest_last_bar_trading,
       to_char(max(c.last_day) FILTER (WHERE c.expiry IS NULL OR (c.expiry >= t.today AND c.expiry > c.last_day)), 'DD Mon') AS newest_last_bar_trading
FROM c, t GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== NB2. NFO futures by expiry (counts only): futures and underlyings tracked for backfill, coverage per timeframe, in the chart catalog, in Data Backfill watchlists'
SELECT to_char(s.expiry, 'DD Mon YYYY') AS expiry,
       CASE WHEN coalesce(s.underlying_symbol, '') IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50') THEN 'index' ELSE 'stock' END AS kind,
       count(*) AS futures, count(DISTINCT s.underlying_symbol) AS underlyings,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_coverage c WHERE c.symbol_id = s.id AND c.timeframe = '5m')) AS with_5m,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_coverage c WHERE c.symbol_id = s.id AND c.timeframe = '15m')) AS with_15m,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_coverage c WHERE c.symbol_id = s.id AND c.timeframe = '30m')) AS with_30m,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_coverage c WHERE c.symbol_id = s.id AND c.timeframe = '60m')) AS with_60m,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_coverage c WHERE c.symbol_id = s.id AND c.timeframe = '1d')) AS with_1d,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM instruments i WHERE i.exchange = 'NFO' AND i.symbol = s.symbol)) AS in_catalog,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_watchlist_items w WHERE w.symbol_id = s.id)) AS in_watchlists
FROM bf_symbols s
WHERE s.source = 'zerodha_nfo' AND (s.option_type IS NULL OR s.option_type NOT IN ('CE', 'PE'))
GROUP BY 1, 2, s.expiry ORDER BY s.expiry, 2;

\echo
\echo '-- NB2b. Underlyings that had a 29 Sep future: how many also have a 27 Oct / 24 Nov future tracked (counts only)'
WITH sep AS (
  SELECT DISTINCT underlying_symbol FROM bf_symbols
  WHERE source = 'zerodha_nfo' AND (option_type IS NULL OR option_type NOT IN ('CE', 'PE')) AND expiry = DATE '2026-09-29'
)
SELECT count(*) AS underlyings_with_sep_future,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM bf_symbols s WHERE s.source = 'zerodha_nfo' AND s.underlying_symbol = sep.underlying_symbol
                                        AND (s.option_type IS NULL OR s.option_type NOT IN ('CE', 'PE')) AND s.expiry > DATE '2026-09-29')) AS with_a_later_future,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM instruments i JOIN instruments u ON u.id = i.underlying_instrument_id
                                        WHERE i.exchange = 'NFO' AND i.instrument_type = 'future' AND i.expiry > DATE '2026-09-29'
                                          AND u.symbol = sep.underlying_symbol)) AS later_future_in_catalog
FROM sep;

\echo
\echo '== PS1. NIFTY PCR Strategy: TOTAL_OI_PCR (nearest weekly expiry, ATM +/- 20 strikes of 50, both OIs present) at each 15-minute close 09:45-15:15, last 3 sessions, beside the 4-expiry PCR; the state a FLAT strategy would enter'
WITH snaps AS (
  SELECT s.id, s.ts, s.session_date, s.spot, s.pcr AS pcr_4exp, (s.expiries ->> 0)::date AS nearest,
         floor(s.spot / 50 + 0.5) * 50 AS atm
  FROM pcr_snapshots s
  WHERE s.underlying = 'NIFTY' AND s.session_date IN (
          SELECT DISTINCT session_date FROM pcr_snapshots WHERE underlying = 'NIFTY' ORDER BY session_date DESC LIMIT 3)
    AND (s.ts AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:45' AND '15:15' AND s.spot IS NOT NULL
), pairs AS (
  SELECT n.id, o.strike,
         max(o.oi) FILTER (WHERE o.option_type = 'CE') AS ce, max(o.oi) FILTER (WHERE o.option_type = 'PE') AS pe
  FROM snaps n JOIN pcr_strike_oi o ON o.snapshot_id = n.id AND o.expiry = n.nearest
  WHERE o.strike BETWEEN n.atm - 1000 AND n.atm + 1000
  GROUP BY n.id, o.strike
), pcr AS (
  SELECT id, count(*) AS listed, count(*) FILTER (WHERE ce IS NOT NULL AND pe IS NOT NULL) AS used,
         sum(pe) FILTER (WHERE ce IS NOT NULL AND pe IS NOT NULL) / nullif(sum(ce) FILTER (WHERE ce IS NOT NULL AND pe IS NOT NULL), 0) AS total_oi_pcr
  FROM pairs GROUP BY id
)
SELECT to_char(n.session_date, 'DD Mon') AS session, to_char(n.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS close,
       to_char(n.nearest, 'DD Mon') AS nearest_expiry, p.used || '/' || p.listed AS strikes,
       round(p.total_oi_pcr::numeric, 3) AS total_oi_pcr, round(n.pcr_4exp::numeric, 3) AS pcr_4_expiries,
       CASE WHEN p.total_oi_pcr IS NULL OR p.used < 0.9 * p.listed THEN 'skip (coverage)'
            WHEN p.total_oi_pcr > 1.25 THEN 'BULLISH' WHEN p.total_oi_pcr < 0.75 THEN 'BEARISH'
            WHEN p.total_oi_pcr BETWEEN 0.80 AND 1.20 THEN 'NEUTRAL' ELSE 'flat (band)' END AS flat_would_enter
FROM snaps n LEFT JOIN pcr p ON p.id = n.id
ORDER BY n.ts;

\echo
\echo '-- PS2. NIFTY PCR Strategy deployments (counts only): per user number, status, regime, last signal and when, closed trades by exit reason'
SELECT u.label AS owner, d.status, d.state::jsonb ->> 'regime' AS regime, d.last_signal,
       to_char(d.last_evaluated_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS last_run_ist,
       (SELECT string_agg(r.exit_reason || ' x' || r.n, ', ') FROM (
          SELECT t.exit_reason, count(*) AS n FROM paper_native_trades t WHERE t.deployment_id = d.id GROUP BY 1) r) AS closed_trades
FROM paper_native_deployments d
JOIN strategy_versions sv ON sv.id = d.strategy_version_id
JOIN paper_portfolios pp ON pp.id = d.portfolio_id
JOIN (SELECT id, 'User ' || row_number() OVER (ORDER BY created_at) AS label FROM users) u ON u.id = pp.user_id
WHERE sv.python_code LIKE '%TOTAL_OI_PCR state machine%'
ORDER BY u.label, d.created_at;

\echo
\echo '== OW1. Who owns what (users numbered by sign-up order, no emails): strategies, capital pools, native deployments in their pools (active), live deployments, broker accounts'
WITH u AS (
  SELECT u.id, 'User ' || row_number() OVER (ORDER BY u.created_at) AS label, u.is_active,
         (SELECT string_agg(r.name, '+' ORDER BY r.name) FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id) AS roles,
         to_char(u.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') AS created, u.email LIKE '%@tradingmaster.internal' AS internal_account
  FROM users u
)
SELECT u.label, u.roles, u.is_active, u.created, u.internal_account,
       (SELECT count(*) FROM strategies s WHERE s.owner_id = u.id) AS strategies,
       (SELECT count(*) FROM paper_portfolios p WHERE p.user_id = u.id) AS pools,
       (SELECT count(*) FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id WHERE p.user_id = u.id) AS native_deployments,
       (SELECT count(*) FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id WHERE p.user_id = u.id AND d.status = 'active') AS native_active,
       (SELECT count(*) FROM paper_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id WHERE p.user_id = u.id) AS simple_paper_deployments,
       (SELECT count(*) FROM live_deployments l WHERE l.owner_id = u.id) AS live_deployments,
       (SELECT count(*) FROM broker_accounts b WHERE b.user_id = u.id) AS broker_accounts
FROM u ORDER BY u.label;

\echo
\echo '== OW2. Every strategy: owner (numbered as OW1), versions, backtests, and its deployments -- whose pool, status'
WITH u AS (
  SELECT u.id, 'User ' || row_number() OVER (ORDER BY u.created_at) AS label, u.is_active,
         (SELECT string_agg(r.name, '+' ORDER BY r.name) FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id) AS roles,
         to_char(u.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') AS created, u.email LIKE '%@tradingmaster.internal' AS internal_account
  FROM users u
)
SELECT s.name AS strategy, s.code_type, owner.label AS owner,
       (SELECT count(*) FROM strategy_versions v WHERE v.strategy_id = s.id) AS versions,
       (SELECT count(*) FROM backtest_jobs b WHERE b.strategy_id = s.id) + (SELECT count(*) FROM native_backtest_jobs b WHERE b.strategy_id = s.id) AS backtests,
       (SELECT string_agg(pu.label || ':' || d.status || ' (' || p.name || ')', ', ' ORDER BY d.created_at)
          FROM paper_native_deployments d JOIN paper_portfolios p ON p.id = d.portfolio_id JOIN u pu ON pu.id = p.user_id WHERE d.strategy_id = s.id) AS native_deployments,
       (SELECT count(*) FROM paper_deployments d WHERE d.strategy_id = s.id) AS simple_paper,
       (SELECT count(*) FROM live_deployments l WHERE l.strategy_id = s.id) AS live
FROM strategies s JOIN u owner ON owner.id = s.owner_id
ORDER BY owner.label, s.name;

\echo
\echo '-- OW3. Capital pools: owner, and how many strategies of other owners run in them'
WITH u AS (
  SELECT u.id, 'User ' || row_number() OVER (ORDER BY u.created_at) AS label, u.is_active,
         (SELECT string_agg(r.name, '+' ORDER BY r.name) FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = u.id) AS roles,
         to_char(u.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY') AS created, u.email LIKE '%@tradingmaster.internal' AS internal_account
  FROM users u
)
SELECT pu.label AS pool_owner, p.name AS pool, round(p.cash::numeric, 0) AS cash, round(p.initial_capital::numeric, 0) AS initial_capital,
       (SELECT count(*) FROM paper_native_deployments d WHERE d.portfolio_id = p.id) AS native_deployments,
       (SELECT count(*) FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id WHERE d.portfolio_id = p.id AND s.owner_id <> p.user_id) AS other_owners_strategies
FROM paper_portfolios p JOIN u pu ON pu.id = p.user_id ORDER BY pu.label, p.name;

\echo
\echo '== Q4. Prices advanced strategies recorded -- entries/exits of trades closed today (IST) and entries of every open holding/leg -- vs the real 5m open at that minute and the last stored close before it (no stock names or prices)'
WITH closed AS (
  SELECT d.strategy_id, 'trade entry' AS kind, t.opened_at AS at, CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id, CASE WHEN l.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'entry_price')::numeric END AS price
  FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE jsonb_typeof(l.value) = 'object' AND (t.opened_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
  UNION ALL
  SELECT d.strategy_id, 'trade exit', t.closed_at, CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END, CASE WHEN l.value->>'exit_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'exit_price')::numeric END
  FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE jsonb_typeof(l.value) = 'object' AND (t.closed_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
), open_legs AS (
  SELECT d.strategy_id, 'open leg entry' AS kind, CASE WHEN d.state::jsonb->'position'->>'opened_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}' THEN (d.state::jsonb->'position'->>'opened_at')::timestamptz END AS at,
         CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id, CASE WHEN l.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'entry_price')::numeric END AS price
  FROM paper_native_deployments d
  CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(d.state::jsonb->'position'->'legs') = 'object' THEN d.state::jsonb->'position'->'legs' ELSE '{}'::jsonb END) l
  WHERE d.status = 'active' AND jsonb_typeof(l.value) = 'object'
  UNION ALL
  SELECT d.strategy_id, 'open holding entry', CASE WHEN h.value->>'opened_at' ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}' THEN (h.value->>'opened_at')::timestamptz END, CASE WHEN h.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (h.value->>'instrument_id')::uuid END, CASE WHEN h.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (h.value->>'entry_price')::numeric END
  FROM paper_native_deployments d
  CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(d.state::jsonb->'holdings') = 'object' THEN d.state::jsonb->'holdings' ELSE '{}'::jsonb END) h
  WHERE d.status = 'active' AND jsonb_typeof(h.value) = 'object'
), ev AS (
  SELECT * FROM closed UNION ALL SELECT * FROM open_legs
), today AS (
  SELECT ev.*, i.instrument_type, i.symbol,
         date_trunc('hour', ev.at) + floor(extract(minute FROM ev.at) / 5) * interval '5 min' AS m5_ts
  FROM ev JOIN instruments i ON i.id = ev.instrument_id
  WHERE ev.kind LIKE 'open%' OR (ev.at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
), priced AS (
  SELECT t.*,
         (SELECT c.low::numeric FROM ohlcv_candles c WHERE c.instrument_id = t.instrument_id AND c.timeframe = '5m' AND c.ts = t.m5_ts) AS real_low,
         (SELECT c.high::numeric FROM ohlcv_candles c WHERE c.instrument_id = t.instrument_id AND c.timeframe = '5m' AND c.ts = t.m5_ts) AS real_high,
         coalesce((SELECT c.open::numeric FROM ohlcv_candles c WHERE c.instrument_id = t.instrument_id AND c.timeframe = '5m' AND c.ts = t.m5_ts),
                  (SELECT x.open::numeric FROM bf_ohlcv_bars x JOIN bf_symbols s ON s.id = x.symbol_id
                    WHERE s.symbol = t.symbol AND s.source IN ('zerodha', 'zerodha_nfo') AND x.timeframe = '5m' AND x.ts = t.m5_ts)) AS real_open,
         (SELECT c.close::numeric FROM ohlcv_candles c WHERE c.instrument_id = t.instrument_id AND c.created_at <= t.at ORDER BY c.ts DESC LIMIT 1) AS last_close,
         (SELECT to_char(c.ts AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM ohlcv_candles c WHERE c.instrument_id = t.instrument_id AND c.created_at <= t.at ORDER BY c.ts DESC LIMIT 1) AS last_close_candle
  FROM today t
)
SELECT s.name AS strategy, p.kind, p.instrument_type, to_char(p.at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS at_ist,
       round((p.price - p.real_open) / nullif(p.real_open, 0) * 100, 2) AS pct_off_real_5m_open,
       p.price BETWEEN p.real_low AND p.real_high AS within_real_5m_range,
       p.last_close_candle AS last_stored_candle_then, abs(p.price - p.last_close) < 0.01 AS price_is_that_stored_close,
       round((p.price - p.last_close) / nullif(p.last_close, 0) * 100, 2) AS pct_off_that_close
FROM priced p JOIN strategies s ON s.id = p.strategy_id
ORDER BY 1, p.at;

\echo
\echo '-- Advanced-strategy names as stored (exact bytes), with their closed trades'
SELECT s.name, length(s.name) AS chars, encode(convert_to(s.name, 'UTF8'), 'hex') AS utf8_hex,
       (SELECT count(*) FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id WHERE d.strategy_id = s.id) AS closed_trades
FROM strategies s WHERE s.code_type = 'native' ORDER BY 1;
\echo '== Q5. MACD - RSI - 15 MIN closed trades: each entry and exit vs the real 5m candle at that minute (no stock names or prices)'
WITH legs AS (
  SELECT t.id AS trade_id, t.opened_at, t.closed_at, t.pnl,
         CASE WHEN l.value->>'instrument_id' ~ '^[0-9a-fA-F-]{36}$' THEN (l.value->>'instrument_id')::uuid END AS instrument_id,
         CASE WHEN l.value->>'entry_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'entry_price')::numeric END AS entry_price,
         CASE WHEN l.value->>'exit_price' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'exit_price')::numeric END AS exit_price,
         CASE WHEN l.value->>'quantity' ~ '^-?\d+(\.\d+)?$' THEN (l.value->>'quantity')::numeric END AS quantity
  FROM paper_native_trades t
  JOIN paper_native_deployments d ON d.id = t.deployment_id JOIN strategies s ON s.id = d.strategy_id
  CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(t.legs::jsonb) = 'array' THEN t.legs::jsonb ELSE '[]'::jsonb END) l
  WHERE s.name ILIKE 'MACD%RSI%15%MIN%' AND jsonb_typeof(l.value) = 'object'
), m5 AS (
  SELECT legs.*, i.symbol,
         date_trunc('hour', legs.opened_at) + floor(extract(minute FROM legs.opened_at) / 5) * interval '5 min' AS entry_ts,
         date_trunc('hour', legs.closed_at) + floor(extract(minute FROM legs.closed_at) / 5) * interval '5 min' AS exit_ts
  FROM legs JOIN instruments i ON i.id = legs.instrument_id
), real AS (
  SELECT m5.*, e.open AS e_open, e.low AS e_low, e.high AS e_high, x.open AS x_open, x.low AS x_low, x.high AS x_high
  FROM m5
  LEFT JOIN LATERAL (
    SELECT * FROM (
      SELECT c.open::numeric, c.low::numeric, c.high::numeric, 1 AS pref FROM ohlcv_candles c
      WHERE c.instrument_id = m5.instrument_id AND c.timeframe = '5m' AND c.ts = m5.entry_ts
      UNION ALL
      SELECT b.open::numeric, b.low::numeric, b.high::numeric, 2 FROM bf_ohlcv_bars b JOIN bf_symbols bs ON bs.id = b.symbol_id
      WHERE bs.source = 'zerodha' AND bs.symbol = m5.symbol AND b.timeframe = '5m' AND b.ts = m5.entry_ts
    ) q ORDER BY pref LIMIT 1) e ON true
  LEFT JOIN LATERAL (
    SELECT * FROM (
      SELECT c.open::numeric, c.low::numeric, c.high::numeric, 1 AS pref FROM ohlcv_candles c
      WHERE c.instrument_id = m5.instrument_id AND c.timeframe = '5m' AND c.ts = m5.exit_ts
      UNION ALL
      SELECT b.open::numeric, b.low::numeric, b.high::numeric, 2 FROM bf_ohlcv_bars b JOIN bf_symbols bs ON bs.id = b.symbol_id
      WHERE bs.source = 'zerodha' AND bs.symbol = m5.symbol AND b.timeframe = '5m' AND b.ts = m5.exit_ts
    ) q ORDER BY pref LIMIT 1) x ON true
)
SELECT row_number() OVER (ORDER BY opened_at) AS trade_no,
       to_char(opened_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS bought_ist,
       round((entry_price - e_open) / nullif(e_open, 0) * 100, 2) AS entry_pct_off_5m_open,
       entry_price BETWEEN e_low AND e_high AS entry_in_real_range,
       to_char(closed_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI:SS') AS sold_ist,
       round((exit_price - x_open) / nullif(x_open, 0) * 100, 2) AS exit_pct_off_5m_open,
       exit_price BETWEEN x_low AND x_high AS exit_in_real_range,
       round(((exit_price - entry_price) - (x_open - e_open)) / nullif(e_open, 0) * 100, 2) AS pnl_error_pct_of_cost
FROM real ORDER BY opened_at;

\echo
\echo '== R1. Backfill Step 3 check: 15m/30m/60m candles built from the saved 5m candles vs Kite''s own, last 14 days (counts only)'
WITH tf(timeframe, mins) AS (VALUES ('15m', 15), ('30m', 30), ('60m', 60)),
five AS (
  SELECT b.symbol_id, b.ts, b.open, b.high, b.low, b.close, b.volume, b.open_interest,
         date_trunc('day', b.ts) + interval '9 hours 15 minutes' AS day_open
  FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
  WHERE s.source IN ('zerodha', 'zerodha_nfo') AND b.timeframe = '5m' AND b.ts >= date_trunc('day', now()) - interval '14 days'
), built AS (
  SELECT f.symbol_id, tf.timeframe,
         f.day_open + floor(extract(epoch FROM f.ts - f.day_open) / 60 / tf.mins) * tf.mins * interval '1 minute' AS ts,
         (array_agg(f.open ORDER BY f.ts))[1] AS open, max(f.high) AS high, min(f.low) AS low,
         (array_agg(f.close ORDER BY f.ts DESC))[1] AS close, sum(f.volume) AS volume,
         (array_agg(f.open_interest ORDER BY f.ts DESC))[1] AS oi
  FROM five f CROSS JOIN tf
  GROUP BY 1, 2, 3
), kite AS (
  SELECT b.symbol_id, b.timeframe, b.ts, b.open, b.high, b.low, b.close, b.volume, b.open_interest AS oi
  FROM bf_ohlcv_bars b
  WHERE b.timeframe IN ('15m', '30m', '60m') AND b.ts >= date_trunc('day', now()) - interval '14 days'
    AND b.symbol_id IN (SELECT DISTINCT symbol_id FROM five)
), kdays AS (
  SELECT DISTINCT symbol_id, timeframe, ts::date AS d FROM kite
), fdays AS (
  SELECT DISTINCT symbol_id, ts::date AS d FROM five
), b AS (  -- only days Kite has this timeframe for too
  SELECT built.* FROM built JOIN kdays USING (symbol_id, timeframe) WHERE kdays.d = built.ts::date
), k AS (  -- only days with saved 5m candles
  SELECT kite.* FROM kite JOIN fdays USING (symbol_id) WHERE fdays.d = kite.ts::date
), cmp AS (
  SELECT coalesce(b.symbol_id, k.symbol_id) AS symbol_id, coalesce(b.timeframe, k.timeframe) AS timeframe, coalesce(b.ts, k.ts) AS ts,
         b.ts IS NOT NULL AS has_built, k.ts IS NOT NULL AS has_kite,
         abs(b.open - k.open) < 0.001 AS o_eq, abs(b.high - k.high) < 0.001 AS h_eq, abs(b.low - k.low) < 0.001 AS l_eq,
         abs(b.close - k.close) < 0.001 AS c_eq, b.volume IS NOT DISTINCT FROM k.volume AS v_eq, b.oi IS NOT DISTINCT FROM k.oi AS oi_eq
  FROM b FULL JOIN k ON k.symbol_id = b.symbol_id AND k.timeframe = b.timeframe AND k.ts = b.ts
)
SELECT s.source, CASE WHEN s.option_type IN ('CE', 'PE') THEN 'option' WHEN s.source = 'zerodha_nfo' THEN 'future' ELSE 'stock/index' END AS kind,
       c.timeframe, count(DISTINCT c.symbol_id) AS symbols, count(DISTINCT c.ts::date) AS days,
       count(*) FILTER (WHERE has_kite) AS kite_candles, count(*) FILTER (WHERE has_built) AS built_candles,
       count(*) FILTER (WHERE has_kite AND NOT has_built) AS only_kite, count(*) FILTER (WHERE has_built AND NOT has_kite) AS only_built,
       count(*) FILTER (WHERE o_eq AND h_eq AND l_eq AND c_eq) AS ohlc_same,
       count(*) FILTER (WHERE o_eq AND h_eq AND l_eq AND c_eq AND v_eq) AS ohlcv_same,
       count(*) FILTER (WHERE o_eq AND h_eq AND l_eq AND c_eq AND v_eq AND oi_eq) AS all_same,
       count(*) FILTER (WHERE NOT o_eq) AS open_diff, count(*) FILTER (WHERE NOT h_eq) AS high_diff, count(*) FILTER (WHERE NOT l_eq) AS low_diff,
       count(*) FILTER (WHERE NOT c_eq) AS close_diff, count(*) FILTER (WHERE NOT v_eq) AS volume_diff, count(*) FILTER (WHERE NOT oi_eq) AS oi_diff
FROM cmp c JOIN bf_symbols s ON s.id = c.symbol_id
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

\echo
\echo '-- R1b. Where the differences are: candle start time (IST) of candles that differ or exist on one side only, top 12'
WITH tf(timeframe, mins) AS (VALUES ('15m', 15), ('30m', 30), ('60m', 60)),
five AS (
  SELECT b.symbol_id, b.ts, b.open, b.high, b.low, b.close, b.volume, b.open_interest,
         date_trunc('day', b.ts) + interval '9 hours 15 minutes' AS day_open
  FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
  WHERE s.source IN ('zerodha', 'zerodha_nfo') AND b.timeframe = '5m' AND b.ts >= date_trunc('day', now()) - interval '14 days'
), built AS (
  SELECT f.symbol_id, tf.timeframe,
         f.day_open + floor(extract(epoch FROM f.ts - f.day_open) / 60 / tf.mins) * tf.mins * interval '1 minute' AS ts,
         (array_agg(f.open ORDER BY f.ts))[1] AS open, max(f.high) AS high, min(f.low) AS low,
         (array_agg(f.close ORDER BY f.ts DESC))[1] AS close, sum(f.volume) AS volume
  FROM five f CROSS JOIN tf
  GROUP BY 1, 2, 3
), kite AS (
  SELECT b.symbol_id, b.timeframe, b.ts, b.open, b.high, b.low, b.close, b.volume
  FROM bf_ohlcv_bars b
  WHERE b.timeframe IN ('15m', '30m', '60m') AND b.ts >= date_trunc('day', now()) - interval '14 days'
    AND b.symbol_id IN (SELECT DISTINCT symbol_id FROM five)
), kdays AS (
  SELECT DISTINCT symbol_id, timeframe, ts::date AS d FROM kite
), fdays AS (
  SELECT DISTINCT symbol_id, ts::date AS d FROM five
), b AS (
  SELECT built.* FROM built JOIN kdays USING (symbol_id, timeframe) WHERE kdays.d = built.ts::date
), k AS (
  SELECT kite.* FROM kite JOIN fdays USING (symbol_id) WHERE fdays.d = kite.ts::date
)
SELECT coalesce(b.timeframe, k.timeframe) AS timeframe, to_char(coalesce(b.ts, k.ts), 'HH24:MI') AS candle_ist,
       count(*) FILTER (WHERE b.ts IS NULL) AS only_kite, count(*) FILTER (WHERE k.ts IS NULL) AS only_built,
       count(*) FILTER (WHERE b.ts IS NOT NULL AND k.ts IS NOT NULL) AS differ
FROM b FULL JOIN k ON k.symbol_id = b.symbol_id AND k.timeframe = b.timeframe AND k.ts = b.ts
WHERE b.ts IS NULL OR k.ts IS NULL
   OR NOT (abs(b.open - k.open) < 0.001 AND abs(b.high - k.high) < 0.001 AND abs(b.low - k.low) < 0.001 AND abs(b.close - k.close) < 0.001
           AND b.volume IS NOT DISTINCT FROM k.volume)
GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 12;

\echo
\echo '== R2. NFO top-up jobs failed "not found" in the latest scheduled run: by underlying, kind and expiry (counts; example contracts for index underlyings only)'
WITH runs AS (
  SELECT id, row_number() OVER (ORDER BY created_at DESC) AS n FROM bf_backfill_runs WHERE source = 'zerodha_nfo' AND kind = 'scheduled'
), failed AS (
  SELECT j.symbol_id, j.timeframe, j.start_date, r.n FROM bf_backfill_jobs j JOIN runs r ON r.id = j.run_id
  WHERE r.n <= 2 AND j.status = 'failed' AND j.error_message ILIKE '%not found%'
)
SELECT s.underlying_symbol, coalesce(s.option_type, 'FUT') AS kind, s.expiry, to_char(s.expiry, 'Dy') AS expiry_day, f.timeframe,
       count(*) AS jobs, count(DISTINCT s.id) AS contracts,
       count(*) FILTER (WHERE f.start_date IS NULL) AS never_had_bars,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM failed p WHERE p.n = 2 AND p.symbol_id = f.symbol_id)) AS failed_run_before_too,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM instruments i WHERE i.exchange = 'NFO' AND i.symbol = s.symbol AND i.is_active)) AS active_in_app_catalog,
       min(s.created_at)::date AS added_first, max(s.created_at)::date AS added_last,
       CASE WHEN s.underlying_symbol IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50')
            THEN (array_agg(s.symbol ORDER BY s.symbol))[1] || ' .. ' || (array_agg(s.symbol ORDER BY s.symbol DESC))[1] END AS examples
FROM failed f JOIN bf_symbols s ON s.id = f.symbol_id
WHERE f.n = 1
GROUP BY 1, 2, 3, 4, 5 ORDER BY 3, 1, 2;

\echo
\echo '-- R2b. The same failures in the NSE top-up (counts only)'
SELECT j.timeframe, count(*) AS jobs, count(DISTINCT j.symbol_id) AS symbols,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM instruments i WHERE i.exchange = 'NSE' AND i.symbol = s.symbol AND i.is_active)) AS active_in_app_catalog,
       max(c.last_ts)::date AS last_saved_bar
FROM bf_backfill_jobs j JOIN bf_symbols s ON s.id = j.symbol_id
LEFT JOIN bf_coverage c ON c.symbol_id = j.symbol_id AND c.timeframe = j.timeframe
WHERE j.run_id = (SELECT id FROM bf_backfill_runs WHERE source = 'zerodha' AND kind = 'scheduled' ORDER BY created_at DESC LIMIT 1)
  AND j.status = 'failed'
GROUP BY 1 ORDER BY 1;

\echo
\echo '== R3. Chart-table candles (what strategies read) vs Kite''s final candle (the backfill copy downloaded in the evening), last 8 days: by how soon after closing the chart candle was saved (counts only)'
WITH tfm(timeframe, mins) AS (VALUES ('5m', 5), ('15m', 15), ('30m', 30), ('60m', 60)),
paired AS (
  SELECT c.timeframe, c.source, c.ts, c.open, c.high, c.low, c.close, c.volume,
         extract(epoch FROM c.created_at - (c.ts + tfm.mins * interval '1 minute')) AS saved_after_s,
         b.open AS b_open, b.high AS b_high, b.low AS b_low, b.close AS b_close, b.volume AS b_volume
  FROM ohlcv_candles c JOIN tfm USING (timeframe) JOIN instruments i ON i.id = c.instrument_id
  JOIN bf_symbols s ON s.source = CASE WHEN i.exchange = 'NFO' THEN 'zerodha_nfo' ELSE 'zerodha' END AND s.symbol = i.symbol
  JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = c.timeframe AND b.ts = c.ts
  WHERE i.exchange IN ('NSE', 'NFO') AND i.data_source = 'zerodha_kite' AND c.ts >= date_trunc('day', now()) - interval '8 days'
)
SELECT timeframe,
       CASE WHEN saved_after_s < 10 THEN '1: under 10 s' WHEN saved_after_s < 60 THEN '2: 10-60 s' WHEN saved_after_s < 300 THEN '3: 1-5 min'
            WHEN saved_after_s < 3600 THEN '4: 5-60 min' ELSE '5: later' END AS saved_after_close,
       count(*) AS candles,
       count(*) FILTER (WHERE NOT (abs(open - b_open) < 0.001 AND abs(high - b_high) < 0.001 AND abs(low - b_low) < 0.001 AND abs(close - b_close) < 0.001)) AS price_differs,
       count(*) FILTER (WHERE abs(close - b_close) >= 0.001) AS close_differs,
       count(*) FILTER (WHERE abs(high - b_high) >= 0.001 OR abs(low - b_low) >= 0.001) AS high_low_differs,
       count(*) FILTER (WHERE volume IS DISTINCT FROM b_volume) AS volume_differs,
       round(avg(abs(close - b_close) / nullif(b_close, 0) * 100) FILTER (WHERE abs(close - b_close) >= 0.001)::numeric, 3) AS avg_close_diff_pct,
       round(max(abs(close - b_close) / nullif(b_close, 0) * 100)::numeric, 3) AS max_close_diff_pct
FROM paired GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '-- R3b. Chart candles saved within 5 minutes of closing whose price differs from Kite''s final: by writer (source) and candle time (IST), top 15'
WITH tfm(timeframe, mins) AS (VALUES ('5m', 5), ('15m', 15), ('30m', 30), ('60m', 60)),
paired AS (
  SELECT c.timeframe, c.source, c.ts, c.open, c.high, c.low, c.close,
         extract(epoch FROM c.created_at - (c.ts + tfm.mins * interval '1 minute')) AS saved_after_s,
         b.open AS b_open, b.high AS b_high, b.low AS b_low, b.close AS b_close
  FROM ohlcv_candles c JOIN tfm USING (timeframe) JOIN instruments i ON i.id = c.instrument_id
  JOIN bf_symbols s ON s.source = CASE WHEN i.exchange = 'NFO' THEN 'zerodha_nfo' ELSE 'zerodha' END AND s.symbol = i.symbol
  JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = c.timeframe AND b.ts = c.ts
  WHERE i.exchange IN ('NSE', 'NFO') AND i.data_source = 'zerodha_kite' AND c.ts >= date_trunc('day', now()) - interval '8 days'
)
SELECT timeframe, source, to_char(ts, 'HH24:MI') AS candle_ist, count(*) AS candles,
       round(avg(saved_after_s)::numeric, 1) AS avg_saved_after_s
FROM paired
WHERE saved_after_s < 300
  AND NOT (abs(open - b_open) < 0.001 AND abs(high - b_high) < 0.001 AND abs(low - b_low) < 0.001 AND abs(close - b_close) < 0.001)
GROUP BY 1, 2, 3 ORDER BY count(*) DESC LIMIT 15;

\echo
\echo '-- R3c. Chart candles that differ from Kite''s final, by writer (source), timeframe and how soon after closing they were saved; which fields differ (counts only)'
WITH tfm(timeframe, mins) AS (VALUES ('5m', 5), ('15m', 15), ('30m', 30), ('60m', 60)),
paired AS (
  SELECT c.timeframe, c.source, i.exchange, i.instrument_type,
         extract(epoch FROM c.created_at - (c.ts + tfm.mins * interval '1 minute')) AS saved_after_s,
         abs(c.open - b.open) >= 0.001 AS o_diff, abs(c.high - b.high) >= 0.001 AS h_diff, abs(c.low - b.low) >= 0.001 AS l_diff,
         abs(c.close - b.close) >= 0.001 AS c_diff, c.volume IS DISTINCT FROM b.volume AS v_diff
  FROM ohlcv_candles c JOIN tfm USING (timeframe) JOIN instruments i ON i.id = c.instrument_id
  JOIN bf_symbols s ON s.source = CASE WHEN i.exchange = 'NFO' THEN 'zerodha_nfo' ELSE 'zerodha' END AND s.symbol = i.symbol
  JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = c.timeframe AND b.ts = c.ts
  WHERE i.exchange IN ('NSE', 'NFO') AND i.data_source = 'zerodha_kite' AND c.ts >= date_trunc('day', now()) - interval '8 days'
)
SELECT timeframe, source, exchange, instrument_type,
       CASE WHEN saved_after_s < 0 THEN '0: before it closed' WHEN saved_after_s < 60 THEN '1: within a minute' WHEN saved_after_s < 3600 THEN '2: within the hour'
            ELSE '3: later' END AS saved_after_close,
       count(*) AS candles, count(*) FILTER (WHERE o_diff OR h_diff OR l_diff OR c_diff OR v_diff) AS differ,
       count(*) FILTER (WHERE o_diff) AS open_d, count(*) FILTER (WHERE h_diff) AS high_d, count(*) FILTER (WHERE l_diff) AS low_d,
       count(*) FILTER (WHERE c_diff) AS close_d, count(*) FILTER (WHERE v_diff) AS volume_d
FROM paired GROUP BY 1, 2, 3, 4, 5 HAVING count(*) FILTER (WHERE o_diff OR h_diff OR l_diff OR c_diff OR v_diff) > 0
ORDER BY 7 DESC LIMIT 20;

\echo
\echo '-- R3e. Live-feed (kite_live) option candles still differing from Kite''s final, by candle day: was the contract copied after its last download? (counts only)'
WITH lj AS (
  SELECT symbol_id, max(completed_at) AS last_download
  FROM bf_backfill_jobs WHERE status = 'completed' OR inserted_count > 0 GROUP BY symbol_id
)
SELECT (c.ts AT TIME ZONE 'Asia/Kolkata')::date AS candle_day, count(*) AS candles, count(DISTINCT c.instrument_id) AS contracts,
       count(*) FILTER (WHERE s.last_synced_at IS NULL) AS never_copied,
       count(*) FILTER (WHERE lj.last_download > s.last_synced_at) AS downloaded_after_last_copy,
       count(*) FILTER (WHERE lj.last_download <= s.last_synced_at) AS copied_after_last_download,
       count(*) FILTER (WHERE lj.last_download IS NULL) AS no_download_job,
       to_char(min(lj.last_download) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_download_min,
       to_char(max(lj.last_download) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_download_max,
       to_char(min(s.last_synced_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS copied_min,
       to_char(max(s.last_synced_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS copied_max,
       count(DISTINCT i.id) FILTER (WHERE (SELECT count(*) FROM instruments i2 WHERE i2.exchange = i.exchange AND i2.symbol = i.symbol) > 1) AS duplicate_catalog_rows
FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
JOIN bf_symbols s ON s.source = 'zerodha_nfo' AND s.symbol = i.symbol
JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = c.timeframe AND b.ts = c.ts
LEFT JOIN lj ON lj.symbol_id = s.id
WHERE c.source = 'kite_live' AND c.timeframe = '15m' AND i.exchange = 'NFO' AND c.ts >= date_trunc('day', now()) - interval '8 days'
  AND (abs(c.close - b.close) >= 0.001 OR c.volume IS DISTINCT FROM b.volume)
GROUP BY 1 ORDER BY 1;

\echo
\echo '-- R3d. NSE 15m chart candles saved before their candle closed (last 8 days): minutes after the candle opened when saved, by day and candle time; how far their close is from Kite''s final (counts only)'
WITH early AS (
  SELECT c.ts, c.created_at, c.instrument_id, extract(epoch FROM c.created_at - c.ts) / 60 AS saved_min, c.close, b.close AS b_close
  FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  JOIN bf_symbols s ON s.source = 'zerodha' AND s.symbol = i.symbol
  JOIN bf_ohlcv_bars b ON b.symbol_id = s.id AND b.timeframe = '15m' AND b.ts = c.ts
  WHERE i.exchange = 'NSE' AND c.timeframe = '15m' AND c.source = i.data_source AND i.data_source = 'zerodha_kite'
    AND c.ts >= date_trunc('day', now()) - interval '8 days' AND c.created_at < c.ts + interval '15 minutes'
)
SELECT ts::date AS day, count(*) AS candles, count(DISTINCT instrument_id) AS stocks,
       count(*) FILTER (WHERE saved_min < 0) AS saved_before_open, count(*) FILTER (WHERE saved_min >= 0 AND saved_min < 5) AS in_min_0_5,
       count(*) FILTER (WHERE saved_min >= 5 AND saved_min < 10) AS in_min_5_10, count(*) FILTER (WHERE saved_min >= 10) AS in_min_10_15,
       round(avg(saved_min)::numeric, 1) AS avg_min, round(min(saved_min)::numeric, 1) AS min_min,
       to_char(min(created_at), 'HH24:MI') AS first_saved, to_char(max(created_at), 'HH24:MI') AS last_saved,
       count(*) FILTER (WHERE abs(close - b_close) >= 0.001) AS close_differs,
       round(avg(abs(close - b_close) / nullif(b_close, 0) * 100)::numeric, 3) AS avg_close_diff_pct
FROM early GROUP BY 1 ORDER BY 1;

\echo
\echo '== M. Database and table sizes'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows_estimate
FROM pg_stat_user_tables
WHERE relname IN ('bf_ohlcv_bars', 'bf_backfill_jobs', 'bf_symbols', 'ohlcv_candles', 'instruments', 'pcr_snapshots', 'pcr_strike_oi')
ORDER BY pg_total_relation_size(relid) DESC;

\echo
\echo '== AM7. AM OP TRD 15 MIN today (IST): legs opened/closed (time, contract, side -- no prices), closed trades (times, exit reason), the open position (regime, NIFTY at entry, since) and its last check; NIFTY 50 candles and the NIFTY level saved at each 15-minute PCR mark, 09:15-11:30. Version 7 (5 Oct 15:39) rolled only on a completed 15-minute close 100+ points from entry; version 8 (5 Oct evening) is version 6 again: it rolls the moment live NIFTY is 100 points from entry'
WITH today AS (SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata' AS start)
SELECT to_char(a.created_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS at_ist, replace(a.action, 'PAPER_NATIVE_LEG_', '') AS leg,
       a.new_value::jsonb ->> 'instrument' AS contract, a.new_value::jsonb ->> 'side' AS side
FROM audit_logs a JOIN paper_native_deployments d ON a.object_id = d.id::text JOIN strategies s ON s.id = d.strategy_id, today
WHERE trim(s.name) = 'AM OP TRD 15 MIN' AND a.object_type = 'paper_native_deployment' AND a.action LIKE 'PAPER_NATIVE_LEG_%'
  AND a.created_at >= today.start
ORDER BY a.created_at;

WITH today AS (SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata' AS start)
SELECT to_char(t.opened_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS opened_ist, to_char(t.closed_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS closed_ist,
       t.exit_reason, json_array_length(t.legs::json) AS legs
FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id JOIN strategies s ON s.id = d.strategy_id, today
WHERE trim(s.name) = 'AM OP TRD 15 MIN' AND t.closed_at >= today.start
ORDER BY t.closed_at;

SELECT d.status, d.state::jsonb -> 'position' ->> 'regime' AS regime,
       round((d.state::jsonb -> 'position' ->> 'entry_spot')::numeric, 2) AS nifty_at_entry,
       to_char((d.state::jsonb -> 'position' ->> 'opened_at')::timestamptz AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS since_ist,
       to_char(d.last_evaluated_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS last_check_ist, d.last_signal,
       CASE WHEN d.last_signal = 'ERROR' THEN split_part(d.last_signal_reason, ':', 1) ELSE d.last_signal_reason END AS last_reason
FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id
WHERE trim(s.name) = 'AM OP TRD 15 MIN' ORDER BY d.created_at;

WITH today AS (SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata' AS start)
SELECT c.timeframe, to_char(c.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS candle_start_ist,
       round(c.open::numeric, 2) AS open, round(c.high::numeric, 2) AS high, round(c.low::numeric, 2) AS low, round(c.close::numeric, 2) AS close
FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id, today
WHERE i.symbol = 'NIFTY 50' AND c.timeframe IN ('5m', '15m')
  AND c.ts >= today.start + interval '9 hours 15 minutes' AND c.ts < today.start + interval '11 hours 30 minutes'
ORDER BY c.timeframe, c.ts;

WITH today AS (SELECT ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata' AS start)
SELECT to_char(p.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS pcr_mark_ist, to_char(p.captured_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS saved_ist,
       round(p.spot::numeric, 2) AS nifty, round(p.pcr::numeric, 3) AS pcr_4_expiry
FROM pcr_snapshots p, today
WHERE p.underlying = 'NIFTY' AND p.ts >= today.start + interval '9 hours 15 minutes' AND p.ts < today.start + interval '11 hours 30 minutes'
ORDER BY p.ts;

\echo
\echo '== AM8. AM OP TRD 15 MIN saved code vs the repo file nifty_pcr_multi_regime.py (332 lines): which version runs, and the line NUMBERS that differ (no code shown); its roll rule (expected from 6 Oct: version 8, saved_md5 93a247f5..., rolls_on_15min_close f)'
SELECT v.version_number AS runs_version, (SELECT max(version_number) FROM strategy_versions WHERE strategy_id = s.id) AS latest_version,
       to_char(v.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS version_saved_ist,
       array_length(string_to_array(replace(v.python_code, E'\r', ''), E'\n'), 1) AS saved_lines,
       (regexp_match(v.python_code, 'ROLL_TRIGGER = ([0-9.]+)'))[1] AS roll_trigger,
       position('CLOSE_GRACE' in v.python_code) > 0 AS rolls_on_15min_close,
       md5(replace(v.python_code, E'\r', '')) AS saved_md5
FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id JOIN strategy_versions v ON v.id = d.strategy_version_id
WHERE trim(s.name) = 'AM OP TRD 15 MIN' ORDER BY d.created_at;

WITH code AS (
  SELECT d.id, v.python_code FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id
  JOIN strategy_versions v ON v.id = d.strategy_version_id WHERE trim(s.name) = 'AM OP TRD 15 MIN' ORDER BY d.created_at LIMIT 1
), saved AS (
  SELECT n AS line_no, md5(l) AS h, length(l) AS len
  FROM code, unnest(string_to_array(replace(code.python_code, E'\r', ''), E'\n')) WITH ORDINALITY AS u(l, n)
), repo AS (
  SELECT n AS line_no, h FROM unnest(ARRAY['d7ef7076734ae84447992034ead7723d','caac6ab3b5adec6908aecf21f16b79b1','ff92b37f0af7bf7e67c8a6fdc779bd1b','d41d8cd98f00b204e9800998ecf8427e','160095c01d2b199d663f6afd83d3ace7','5aececbe24fd11dc3d4242b76d4d2887','d41d8cd98f00b204e9800998ecf8427e','6d18ae6960e15afcfe4fe1e70e3d731b','bb4993552215f6262e00f5e6f571e905','0699ed4842c76f35a894101a5cdfd6d3','de73b7011e7f2adffb1752a502afb6f0','c23ec5620e7a05ecffff79f242161962','69cdaa5afada4b654c73b37419f08319','48c90db57cb794670fd39211ac7e7655','d41d8cd98f00b204e9800998ecf8427e','826b981899e17179f17922defe140417','bfe71f2f9c3ef585355894ffc1c10464','cfd8c9815f0b301183bcd6e65f7609fa','d41d8cd98f00b204e9800998ecf8427e','439b88339f7cd8dca74115ce56f7a8a1','3cdc19200f7d70e56511b0c54ab5bcd1','fff6d731a69b6c427d17b583840f0bc8','d41d8cd98f00b204e9800998ecf8427e','c9932e21a48feb80a2d3e98ac91dce4e','a240acc2322a224b4f14aff1e9992c23','50d680c52d5d9e5ceabf40607fddd99f','d41d8cd98f00b204e9800998ecf8427e','4268e2b857495161d01a22c9319adae9','2c9b35fdf151b78a082f6efa4d10ae20','23e49e03fb207088f76505aeeadcce49','a17a4669e353a4376d345d84f5014b79','5df044ca16d59f750dd3f321113c5931','df03757ee1dc096ca305c37635224ed7','b450d07903e08bf6459013672f53610a','73c4b8109e1cf0e26d1c0ee60cb3e660','d3325babb18cda18f2db2715e94df48c','e83f1170214eb04abc3bbb23a0dea452','74e628a98928381874f76625b0f8f54b','d41d8cd98f00b204e9800998ecf8427e','76bdbc4d5997d7175acd8aac782fd1a9','8809ff238193ad508fcce53724a0a73b','2fd3bf02f590aba02ea5e4111091faef','2005f764b4151319836d39e6c24090b3','439b1fcc009b1f678280a2f4d837ac54','c57b9721fd2ac2cff67681def41ef520','72e2ca1d187d2cbabf8f6c69c381ac1a','d41d8cd98f00b204e9800998ecf8427e','ebf13491813b23108e31fdd1445624d8','7a05f091ac5ec475e70aee6cd8714152','365c3ce6787cb8e2169e2ac3cabec83d','541ce1b39d7be811ffaeb67ab1f50044','d41d8cd98f00b204e9800998ecf8427e','ef6a111fd75b67a881e9e8bd16223bc9','dd22512b167b8c9408ebbf9063270dcb','3b13a6e76ba09655f6b5da6c00e0c93c','5615f9d70734a6de401ad99525dac1fd','c130d82c2934f4c5763a36b011c24853','f15684685fbbca56804bd664ddb2db00','ec6059dd80dc526c584a82a9654858d4','d41d8cd98f00b204e9800998ecf8427e','db3916a76b8642c857f1e27d2dc66e70','4f8e7c2bc3828998eb6a336aac6f4e8b','cd93462e5253c2dc4acc08934f829c82','f4a245eb80143ba2ecfec617396f687b','97d590e51498660bbdc4746185420698','cb1a232accfcf080b36a6857b6ee7c3f','d41d8cd98f00b204e9800998ecf8427e','7af269660c01b69b1f6911af7d998baf','27d256c3d164c0b4bc54faf6528e9161','0125019b875f0cc3101beec89590d3a4','bd6f8271172a87576ab96b8ee6eecc90','38cf173be81f853d713497a04f98fba5','d41d8cd98f00b204e9800998ecf8427e','fd2ee1aecc6cd30c93bc93c00c81846b','5a6c7efe4fd15014fce7cb73966a4321','c64afca85896e8d6182f5ce733be5a17','b4a2b2ed8a28d871bddf850a8d6b1788','873ad48cf6e1c71d2ccdcddd7083d6a1','d7ef7076734ae84447992034ead7723d','d41d8cd98f00b204e9800998ecf8427e','0f1b5a0177517360bb269df711667a96','b8867927788c93479edfc55cef2c80be','d41d8cd98f00b204e9800998ecf8427e','074f69dfe6ef6f55891bb04bbeb4dd0a','d41d8cd98f00b204e9800998ecf8427e','46f02925550cb8cfce84336a85050563','a1313ca803bc0479e5fed4cb135b9d2b','d41d8cd98f00b204e9800998ecf8427e','bb22713e89fe0bc905a5aee6b5496b3b','507abd8c3cd97f40ffde6c5feba05579','5e854d95016c1d6cd1311bffc82f08b0','211b510304ba4cf58f68f11c10928c2f','7159cfbadba25520e70e16ef72416777','1c92c2c10c4714e38a7c001d686f6b18','9733ec4fb37a8fba045b6698b66d41e1','9e16e0f39674d70b8b6c68308c956ad7','d41d8cd98f00b204e9800998ecf8427e','7b91f886b4ae1bbd418d8e73b9bc89f8','d41d8cd98f00b204e9800998ecf8427e','f84d4d19b7342438eee97329779e327e','784e2000ea598626fcc28034c45c3ee8','58e68e82f7be7fb9dfb2a2f4b03ed0e6','25f289496f68171718a9eb85e3b8d70f','8832486a5fd6d980cccdc5f02092dd78','875813c24998adcd8d9512c7512df4dc','ba8b0a6358628b96e72cb8dc41582f72','28315f37cc4d800cb28c8caa6df4d0a4','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','c3d95973501be25ded9eba6a95aadb8b','942a7035dda27895f2e3fc821aa4640d','57c77147324f56a2628a50514882c8cc','330e25bc7ef4ad17eb31909c4dd823ae','7f208ba806a794b56337bfd3d06e3245','f362e2e938c21af235c278f561826bc1','925c1eaf1a36d36ab7e4a599de60c798','6b71b4510fd3aea8a18f6bcc5e281735','627ef725e64f5186c298a14c6503d2e3','1688189199aff97a480dcb1384d1fb86','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','15c90f18051550cf2a7c6505b7b97e98','41402ea7db6a85a927a58b76227de442','6acdea2dfcca8e51ba65b563688e5535','fd69cb4f776cea1bf4eb23328328b49c','bf1ed631670357a03209fa4c82df6620','cf38ed55fe565c8eaa4655d286d164c1','c12838068ff7af71de63f2a1267a7dbe','d19a2d9fcc61c94ac3c514c3d704dea9','d1b78743728c649cf1c626d8ccc01342','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','11adee3fc4a1786ea67d00b22a6b3102','d06ae76ad5cbb4680b46da0bb6a85c8f','0e438d4b365cd8193156015e1b6356d8','83885ab45745d9c4c2651f413efbb0e2','ed7723f6320f82a45019c5bd735da4d5','68f44bf486741287fd921dc8a7792d9c','fe5051eaff96322272cabc7f841d170e','994246b3e16964e0f5157d72b1962a55','1bdf72a7619e07394e66b0a146ddc95b','a07fa15eec5f750d47e16e4581161df9','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','19aa7ae90d254799a6fc8173882d7e7f','c65ea234b8593edca838db1e3ee1865a','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','02e820c92333d434543afbc759fd23c6','48d137431d511b5db1dc6cdc2f171e47','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','7e7b48ffdb46485c226000d3c9c8d719','97cafb3e33e16d73c4f1bf797a62425c','83885ab45745d9c4c2651f413efbb0e2','65539b2e3a350f2eaca71a2f957b190d','e60cf3ea74b4548c9dd8b1fb22a6f074','c43fff1649fe9294325a4f2955bde2b1','c356e47c1482d0e4559f469e9a7c7f0a','2877aba4322e765595e5a0eaf6f3d0be','4e1ddba1c31545ee09fb38ae1fd79346','68f44bf486741287fd921dc8a7792d9c','c43fff1649fe9294325a4f2955bde2b1','1bf1d8bbfaf146439174e2d6e9e8e8bd','0073218532fa9f06c327eafe48d585f6','4e1ddba1c31545ee09fb38ae1fd79346','994246b3e16964e0f5157d72b1962a55','c43fff1649fe9294325a4f2955bde2b1','6393ffe10d4bf14f0713e594732b91fa','70ff38dc28972c30e0d6c2a67dd47889','4e1ddba1c31545ee09fb38ae1fd79346','b73ca68f1eca9e790ff1daede8bae2a5','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','a7f46a27f8a432e99d29de92ff7cf0f7','1d9ade99fc863968c7d3906b8f19023b','9ddf34140bc4b2f17f193ccc6b9055ff','d41d8cd98f00b204e9800998ecf8427e','29dc5a3ac917a9195a6b13723e4ad21d','7b8574cb3cadbb0e3a30c821a65515c7','e5efc22167c6ddcbc6885954b9f39b5a','5586a475130e5c065ed59ae486cbe28e','26049857bea1003bd19d54c405cf0fb8','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','c630efab62f05c8e1b7a75f0e87ce46a','e144e4a225c57607410a8c3bfb204da5','d41d8cd98f00b204e9800998ecf8427e','ba837fcab1073fe00868f77020db8fe3','b937930a97697e634386593154be7136','585f0c55edd4670393f0c5fd6b070df1','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','25f3f1e9b1eb3055db904e0726ee2305','64dbda5fa8ed75bb8bbb59c2da3387b0','5a8a96d2f95c5c32b772723a2f014799','0640c0842259ad7751cd6d05f97fc3d5','fe44af31b1b16409459ef4c93a4eb524','81cdf77f49f3139f9f1b27cf7c809aa1','c96a554699392f82057f61735594c2dd','180fc2b7ece1e0c051edc9fd21afeb05','34fb4fc0031a79844c2a10a7f761baed','b9017cbc027c2674b83047a5c17abbb0','3575b3a42d4785ff50108bf698cbde7f','d41d8cd98f00b204e9800998ecf8427e','7bf88eadfcbdab4e530b253819272ad2','26133ea94ad06cc891012ce4b101d9a9','72e45e0a03ecdf6ee189cc86f2bf05d8','8b17348e291938c3295dd0ae30483d17','dcd327a1fb9e7780257fed1267cf97fd','bb7e2e3b85099405ba672c57ea549e20','45fe97cfd9b5ddcde9b6d6e92cd2d0ac','9fc80afdfc6e8ac66217951a9ee23b9d','2af4aa1ddd6c220323c7d8305d35310a','e3c151936781c88c4929f42a36b30d93','28b697dc9959201f3f3b91e6758ecb79','d41d8cd98f00b204e9800998ecf8427e','0b0f79eb804a1dd39be914bc7996328e','da7e226902f73fbc881f8d2b08d57057','45be00827ec1ba2176caf51f3db951c4','dda1c640a177a2a34a08faa4220a149a','415ece355bdeb6b5dc29971def54613c','3c62904f20ffd31f3d873ecf8b98584a','d41d8cd98f00b204e9800998ecf8427e','f98f03dd9b7e58e3644dbfa9463abc65','1bda80abd89869d420116518238a08de','d41d8cd98f00b204e9800998ecf8427e','ea60a360a4810494ebd344379a879536','465eb112dc18e54dc5e58cdf6df847ac','d41d8cd98f00b204e9800998ecf8427e','86892b103cf5739dd7f11d42a2f29158','23f7a386815abc41beceb07669bcec7f','c111dfe53584bdafdadebba93241cbe3','327ce95ded6f7aaa5f03b9c8311a3438','d41d8cd98f00b204e9800998ecf8427e','54da4c8d22df7547ef955724136ed97f','4c9134a2bef702f125c7c37f65b8af9b','d81fee36e855f66439c1787f54c9eb07','327ce95ded6f7aaa5f03b9c8311a3438','d41d8cd98f00b204e9800998ecf8427e','3932dcffb404242d776d40112fc8814f','885b048e011184c06d3252819b7b62db','ef47c505bfe6c1e965f806421d598dc3','62ed27c18074f9ef7a8d1faebc935891','77a9976f61e2b7dd2a381b4c5ba6088f','576027f7ee2df8d1c728bbb4f5a18115','e8478a7c31f79d5f61186bbe2af9cd17','2f2458053b3451bfb3a80671d6af9c1c','d41d8cd98f00b204e9800998ecf8427e','ac3f11f936acfd3b8f6d2efe59a24a21','95184faa74c371bb35db2ffdda232010','c5eeea44fe7e8092b55e2ebd85dab8e8','2e34d0ce0a006696fff2d4f13a7dc717','d58d5ddcfb2b9dd90f2d41407fa19a5e','46919294ca616cdb3d716ca9d3c14218','a958c4c45d836bff3d09456cd362959c','839d2e97ae268a31751d4c2ab42f5fa2','327ce95ded6f7aaa5f03b9c8311a3438','d41d8cd98f00b204e9800998ecf8427e','cb854a6fa64e1a6aeef37f12155c8dd9','cb8a2f3b6dbca18402ce0c207e279bcf','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','c23cba6d31b37fded4de8f2d9aca4b5a','84151ebdb776e4b1e6d69b454aaa47bc','a95d1178c19df3c1366d2281f69fa3b9','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','a21e576657a8c4711e88c670a9c46c64','a3823e45817d8bd46672bb8a466cc4ad','5e1196c57e8b4a8fd88fd211a73ad5b5','4ae54e2d0a30a3f85569e28ca004ebfe','327ce95ded6f7aaa5f03b9c8311a3438','79e63255aff6b8affc1b97e7d94999dc','13df4b8c666104c50439d3cb82f928f5','95cc01356b61b4a2a5afd6d8f423b3de','d41d8cd98f00b204e9800998ecf8427e','aa0b5ebf2a711eef3af29c7549047e76','1d5f507a50b25df7c90936767adade99','43f3747e01b44b5a5135f050df89781c','15ce6bf35f10e26eccc52e0fd1b033c2','97a4e3b581ea70dc86102502736955e7','22bd7e63831ddebaebacfd43ff616987','bad2f9fe2c25661d9e031d4b3ba4f337','46d65229722c346fa320ee893d0adfa9','d41d8cd98f00b204e9800998ecf8427e','aa714d251c5c85ba07dfdf4f0e461d84','d87d75dff4b6f8309bc8954aafe0309e','d41d8cd98f00b204e9800998ecf8427e','1ef3b169bc1621f4785404a04b108999','f5b15d05b48a470613a1ee3db78cdc2a','f1b4643b293cafc35c233b6f62924e80','129ddcfd7c199e34c4eb2601cf54f174','c36d5d8651954c9d4b14e4d75a07f563','327ce95ded6f7aaa5f03b9c8311a3438','e9d0f73325781c74de36bf01aac15ff0','ace9ca4f98dc95627cef9b02fb3dca8e','e617cfd7fc799560f397a6d17f4bb667','327ce95ded6f7aaa5f03b9c8311a3438','4d25c236279a580263bfb573c6a1169e','d41d8cd98f00b204e9800998ecf8427e','e090057702480c633b7db89f58c83e7a','bc61075e2344cf47d1187a8565f2ba09','d41d8cd98f00b204e9800998ecf8427e','deb0c66418f0f503c2b478ebd1d64176','8f6f4f10aee4e365d1c87d84ed640555','d41d8cd98f00b204e9800998ecf8427e','4ed49083cdb91fb3798f405c63048c30','e68e610993b09b5147f45e2ca7fceb16','61062544e0b20712148e6f644c673df6','95af91e0588374139de8641de875aea2','f971450a2c8d92ce1ab7b9551d526d9c','0800325c0ac3a7c50ba0255a9cb8c8af','51cf37b24d62dc8a120f41e7face5678','8d9c2171255d91db0dab7fc15e741a0f','6f466d14aa73b94bfcc5ac6890ebfa5c','9a62ec2bec444cc61c8034df16e9b07c','3495de44ac781b3b6adeaea751f1dd3a','8320ed2d3a5574d6824c7d7f223c041d','44653a2f9f9966d6b3453d9f5b7e7379','3c308a2a916889eeddba1aa5f8751fba','efb3e9ba30f1457461fcf341f1dcb10c','7ddd5a90fd0f28f15e9b9a264dc394f8','dd668b39867072d291e74bddbc42ef17','1eb5603bbf082f3b7ecc93044f39e863','83e4fd3b5f359eebd745154af3b0b8d7','ea5a1cbe764918679997181a3ea281d3','b4a41fec817994465aeb80813646e6b9','4ec20520f7392d781144a212fd3b96d5','f4c59a6d7f00aa98f501f0544785877a','5c398a6968507454ba18432a85a83f63','d41d8cd98f00b204e9800998ecf8427e']::text[]) WITH ORDINALITY AS r(h, n)
)
SELECT 'only in saved' AS which, string_agg(line_no::text, ',' ORDER BY line_no) AS line_numbers, count(*) AS lines
FROM saved WHERE h NOT IN (SELECT h FROM repo)
UNION ALL
SELECT 'only in repo', string_agg(line_no::text, ',' ORDER BY line_no), count(*)
FROM repo WHERE h NOT IN (SELECT h FROM saved);

\echo
\echo '== AM9. 5 Oct, AM OP rollover compared (live 100-point move vs a 15-minute close 100 points away): its legs as filled (contract, side, times, per-unit prices -- no size, no P&L), NIFTY and PCR at each 15-minute close to 15:00, and NIFTY 6-Oct option closes at each 15-minute close (market prices)'
SELECT to_char(t.opened_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS opened_ist, to_char(t.closed_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS closed_ist,
       t.exit_reason, i.symbol AS contract, l.value ->> 'side' AS side,
       round((l.value ->> 'entry_price')::numeric, 2) AS entry, round((l.value ->> 'exit_price')::numeric, 2) AS exit
FROM paper_native_trades t JOIN paper_native_deployments d ON d.id = t.deployment_id JOIN strategies s ON s.id = d.strategy_id
CROSS JOIN LATERAL jsonb_array_elements(t.legs::jsonb) AS l(value)
LEFT JOIN instruments i ON i.id::text = l.value ->> 'instrument_id'
WHERE trim(s.name) = 'AM OP TRD 15 MIN' AND (t.closed_at AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05'
ORDER BY t.closed_at, i.symbol;

SELECT to_char(p.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS close_ist, to_char(p.captured_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS saved_ist,
       round(p.spot::numeric, 2) AS nifty_at_close, round(c.close::numeric, 2) AS nifty_5m_close, round(p.pcr::numeric, 3) AS pcr_4_expiry
FROM pcr_snapshots p
LEFT JOIN instruments n ON n.symbol = 'NIFTY 50'
LEFT JOIN ohlcv_candles c ON c.instrument_id = n.id AND c.timeframe = '5m' AND c.ts = p.ts - interval '5 minutes'
WHERE p.underlying = 'NIFTY' AND (p.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05'
  AND (p.ts AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:45' AND TIME '15:00'
ORDER BY p.ts;

-- AM9b. Which candles exist today for the NIFTY 6-Oct contracts 22200-22800 (main table and backfill copy), by timeframe
SELECT 'chart' AS tbl, c.timeframe, count(*) AS candles, count(DISTINCT c.instrument_id) AS contracts,
       to_char(min(c.ts) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS first_ist, to_char(max(c.ts) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS last_ist
FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
WHERE i.exchange = 'NFO' AND i.symbol LIKE 'NIFTY26O06%' AND i.strike BETWEEN 22200 AND 22800
  AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05'
GROUP BY c.timeframe
UNION ALL
SELECT 'backfill', b.timeframe, count(*), count(DISTINCT b.symbol_id),
       to_char(min(b.ts) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI'), to_char(max(b.ts) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI')
FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
WHERE s.symbol LIKE 'NIFTY26O06%' AND (b.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05'
GROUP BY b.timeframe ORDER BY 1, 2;

-- AM9c. Option closes at each 15-minute close (09:45-15:00): the close of the 5m candle ending then, else the 15m one; chart table first, else the backfill copy
WITH bars AS (
  SELECT i.symbol, c.timeframe, c.ts, c.close FROM ohlcv_candles c JOIN instruments i ON i.id = c.instrument_id
  WHERE i.exchange = 'NFO' AND i.symbol LIKE 'NIFTY26O06%' AND (c.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05' AND c.timeframe IN ('5m', '15m')
  UNION ALL
  SELECT s.symbol, b.timeframe, b.ts, b.close FROM bf_ohlcv_bars b JOIN bf_symbols s ON s.id = b.symbol_id
  WHERE s.symbol LIKE 'NIFTY26O06%' AND (b.ts AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-05' AND b.timeframe IN ('5m', '15m')
), at_close AS (
  SELECT symbol, ts + (CASE timeframe WHEN '5m' THEN interval '5 minutes' ELSE interval '15 minutes' END) AS close_at,
         timeframe, close
  FROM bars
), pick AS (
  SELECT DISTINCT ON (symbol, close_at) symbol, close_at, close FROM at_close
  WHERE extract(minute FROM (close_at AT TIME ZONE 'Asia/Kolkata')) IN (0, 15, 30, 45)
    AND (close_at AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:45' AND TIME '15:00'
  ORDER BY symbol, close_at, timeframe
)
SELECT to_char(close_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS close_ist,
       max(close) FILTER (WHERE symbol = 'NIFTY26O0622400CE') AS ce_22400, max(close) FILTER (WHERE symbol = 'NIFTY26O0622400PE') AS pe_22400,
       max(close) FILTER (WHERE symbol = 'NIFTY26O0622500CE') AS ce_22500, max(close) FILTER (WHERE symbol = 'NIFTY26O0622500PE') AS pe_22500,
       max(close) FILTER (WHERE symbol = 'NIFTY26O0622600CE') AS ce_22600, max(close) FILTER (WHERE symbol = 'NIFTY26O0622600PE') AS pe_22600
FROM pick GROUP BY close_at ORDER BY close_at;


\echo
\echo '== RW1. Nifty RS Rotation (weekly): every strategy whose code is the weekly RS rotation -- versions, the latest one''s fingerprint vs the repo file, its rebalance time, and its deployments'
SELECT s.name AS strategy, v.version_number AS latest_version, to_char(v.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon YYYY HH24:MI') AS saved_ist,
       v.timeframe AS version_timeframe, md5(replace(v.python_code, E'\r', '')) = '817bb15b2e585c9eda39b8c9a8603b7b' AS same_as_repo_file,
       (regexp_match(v.python_code, 'REBALANCE_TIME = time\(([0-9, ]+)\)'))[1] AS rebalance_time,
       position('force_exit' in v.python_code) > 0 AS handles_force_exit,
       (SELECT count(*) FROM native_backtest_jobs b WHERE b.strategy_id = s.id) AS backtests,
       (SELECT string_agg(d.status, ', ' ORDER BY d.created_at) FROM paper_native_deployments d WHERE d.strategy_id = s.id) AS deployments
FROM strategies s
JOIN LATERAL (SELECT * FROM strategy_versions v WHERE v.strategy_id = s.id ORDER BY v.version_number DESC LIMIT 1) v ON true
WHERE v.python_code LIKE '%REBALANCE_WEEKDAY%' AND v.python_code LIKE '%_rebalance_date_for_week%'
ORDER BY s.name;

\echo
\echo '== RW2. Its backtests: when, the range asked for, how each ended (status, run time, trades) -- the error only as its kind, never its text'
SELECT s.name AS strategy, to_char(b.created_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS asked_ist, b.start_date, b.end_date,
       b.end_date - b.start_date AS days, b.status,
       round(extract(epoch FROM (coalesce(b.completed_at, now()) - b.started_at)) / 60.0, 1) AS run_minutes,
       (SELECT (r.metrics->>'trade_count')::int FROM native_backtest_results r WHERE r.job_id = b.id) AS trades,
       (SELECT count(*) FROM native_backtest_trades t WHERE t.job_id = b.id) AS trade_rows,
       CASE WHEN b.error_message IS NULL THEN NULL
            WHEN b.error_message LIKE '%MultipleResultsFound%' THEN 'more than one row found'
            WHEN b.error_message ~* 'timeout|canceling statement' THEN 'database timeout'
            WHEN b.error_message LIKE '%evaluation failed%' THEN 'strategy raised ' || coalesce((regexp_match(b.error_message, 'failed at [^ ]+: ([A-Za-z]+)'))[1], '?')
            ELSE 'other: ' || coalesce((regexp_match(b.error_message, '^([A-Za-z]+):'))[1], '?') END AS error_kind
FROM native_backtest_jobs b JOIN strategies s ON s.id = b.strategy_id
JOIN LATERAL (SELECT python_code FROM strategy_versions v WHERE v.id = b.strategy_version_id) v ON true
WHERE v.python_code LIKE '%REBALANCE_WEEKDAY%' AND v.python_code LIKE '%_rebalance_date_for_week%'
ORDER BY b.created_at DESC LIMIT 25;

\echo
\echo '== RW3. The data it ranks on: NIFTY 50 rows and their daily candles; the universe (500 symbols in the repo file) -- how many resolve to an NSE instrument and how much daily history they have (counts and dates only)'
SELECT i.exchange, i.instrument_type, i.data_source, count(c.*) AS daily_candles,
       min((c.ts AT TIME ZONE 'Asia/Kolkata')::date) AS first_day, max((c.ts AT TIME ZONE 'Asia/Kolkata')::date) AS last_day,
       string_agg(DISTINCT to_char(c.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI'), ',') AS candle_time_ist
FROM instruments i LEFT JOIN ohlcv_candles c ON c.instrument_id = i.id AND c.timeframe = '1d'
WHERE i.symbol = 'NIFTY 50' GROUP BY i.id, i.exchange, i.instrument_type, i.data_source;

WITH uni AS (SELECT unnest(ARRAY['360ONE','3MINDIA','AADHARHFC','AARTIIND','AAVAS','ABB','ABBOTINDIA','ABCAPITAL','ABDL','ABFRL','ABLBL','ABREL','ABSLAMC','ACC','ACE','ACMESOLAR','ACUTAAS','ADANIENSOL','ADANIENT','ADANIGREEN','ADANIPORTS','ADANIPOWER','AEGISLOG','AEGISVOPAK','AFCONS','AFFLE','AIAENG','AIIL','AJANTPHARM','ALKEM','AMBER','AMBUJACEM','ANANDRATHI','ANANTRAJ','ANGELONE','ANTHEM','ANURAS','APARINDS','APLAPOLLO','APOLLOHOSP','APOLLOTYRE','APTUS','ARE&M','ASAHIINDIA','ASHOKLEY','ASIANPAINT','ASTERDM','ASTRAL','ATGL','ATHERENERG','ATUL','AUBANK','AUROPHARMA','AWL','AXISBANK','BAJAJ-AUTO','BAJAJFINSV','BAJAJHFL','BAJAJHLDNG','BAJFINANCE','BALKRISIND','BALRAMCHIN','BANDHANBNK','BANKBARODA','BANKINDIA','BATAINDIA','BAYERCROP','BBTC','BDL','BEL','BELRISE','BEML','BERGEPAINT','BHARATFORG','BHARTIARTL','BHARTIHEXA','BHEL','BIKAJI','BIOCON','BLS','BLUEDART','BLUEJET','BLUESTARCO','BOSCHLTD','BPCL','BRIGADE','BRITANNIA','BSE','BSOFT','CAMS','CANBK','CANFINHOME','CANHLIFE','CAPLIPOINT','CARBORUNIV','CARTRADE','CASTROLIND','CCL','CDSL','CEATLTD','CEMPRO','CENTRALBK','CESC','CGCL','CGPOWER','CHALET','CHAMBLFERT','CHENNPETRO','CHOICEIN','CHOLAFIN','CHOLAHLDNG','CIEINDIA','CIPLA','CLEAN','COALINDIA','COCHINSHIP','COFORGE','COHANCE','COLPAL','CONCOR','CONCORDBIO','COROMANDEL','CPPLUS','CRAFTSMAN','CREDITACC','CRISIL','CROMPTON','CUB','CUMMINSIND','CYIENT','DABUR','DALBHARAT','DATAPATTNS','DCMSHRIRAM','DEEPAKFERT','DEEPAKNTR','DELHIVERY','DEVYANI','DIVISLAB','DIXON','DLF','DMART','DOMS','DRREDDY','ECLERX','EICHERMOT','EIDPARRY','EIHOTEL','ELECON','ELGIEQUIP','EMAMILTD','EMCURE','EMMVEE','ENDURANCE','ENGINERSIN','ENRIN','ERIS','ESCORTS','ETERNAL','EXIDEIND','FACT','FEDERALBNK','FINCABLES','FIRSTCRY','FIVESTAR','FLUOROCHEM','FORCEMOT','FORTIS','FSL','GABRIEL','GAIL','GALLANTT','GESHIP','GICRE','GILLETTE','GLAND','GLAXO','GLENMARK','GMDCLTD','GMRAIRPORT','GODFRYPHLP','GODIGIT','GODREJCP','GODREJIND','GODREJPROP','GPIL','GRANULES','GRAPHITE','GRASIM','GRAVITA','GROWW','GRSE','GVT&D','HAL','HAVELLS','HBLENGINE','HCLTECH','HDBFS','HDFCAMC','HDFCBANK','HDFCLIFE','HEGAM','HEROMOTOCO','HEXT','HFCL','HINDALCO','HINDCOPPER','HINDPETRO','HINDUNILVR','HINDZINC','HOMEFIRST','HONASA','HONAUT','HSCL','HUDCO','HYUNDAI','ICICIAMC','ICICIBANK','ICICIGI','ICICIPRULI','IDBI','IDEA','IDFCFIRSTB','IEX','IFCI','IGIL','IGL','IIFL','IKS','INDGN','INDHOTEL','INDIACEM','INDIAMART','INDIANB','INDIGO','INDUSINDBK','INDUSTOWER','INFY','INOXWIND','INTELLECT','IOB','IOC','IPCALAB','IRB','IRCON','IRCTC','IREDA','IRFC','ITC','ITCHOTELS','ITI','J&KBANK','JAINREC','JBMA','JINDALSAW','JINDALSTEL','JIOFIN','JKCEMENT','JKTYRE','JMFINANCIL','JPPOWER','JSL','JSWCEMENT','JSWDULUX','JSWENERGY','JSWINFRA','JSWSTEEL','JUBLFOOD','JUBLINGREA','JUBLPHARMA','JWL','JYOTICNC','KAJARIACER','KALYANKJIL','KARURVYSYA','KAYNES','KEC','KEI','KFINTECH','KIMS','KIRLOSENG','KOTAKBANK','KPIL','KPITTECH','KPRMILL','LALPATHLAB','LATENTVIEW','LAURUSLABS','LEMONTREE','LENSKART','LGEINDIA','LICHSGFIN','LICI','LINDEINDIA','LLOYDSME','LODHA','LT','LTF','LTFOODS','LTM','LTTS','LUPIN','M&M','M&MFIN','MAHABANK','MANAPPURAM','MANKIND','MAPMYINDIA','MARICO','MARUTI','MAXHEALTH','MAZDOCK','MCX','MEDANTA','MEESHO','MFSL','MGL','MINDACORP','MMTC','MOTHERSON','MOTILALOFS','MPHASIS','MRF','MRPL','MSUMI','MUTHOOTFIN','NAM-INDIA','NATCOPHARM','NATIONALUM','NAUKRI','NAVA','NAVINFLUOR','NBCC','NCC','NESTLEIND','NETWEB','NEULANDLAB','NEWGEN','NH','NHPC','NIACL','NIVABUPA','NLCINDIA','NMDC','NSLNISP','NTPC','NTPCGREEN','NUVAMA','NUVOCO','NYKAA','OBEROIRLTY','OFSS','OIL','OLAELEC','OLECTRA','ONESOURCE','ONGC','PAGEIND','PARADEEP','PATANJALI','PAYTM','PCBL','PERSISTENT','PETRONET','PFC','PFIZER','PFOCUS','PGEL','PHOENIXLTD','PIDILITIND','PIIND','PINELABS','PIRAMALFIN','PNB','PNBHOUSING','POLICYBZR','POLYCAB','POLYMED','POONAWALLA','POWERGRID','POWERINDIA','PPLPHARMA','PREMIERENE','PRESTIGE','PTCIL','PVRINOX','PWL','RADICO','RAILTEL','RAINBOW','RAMCOCEM','RBLBANK','RECLTD','REDINGTON','RELIANCE','RHIM','RITES','RKFORGE','RPOWER','RRKABEL','RVNL','SAGILITY','SAIL','SAILIFE','SAMMAANCAP','SAPPHIRE','SARDAEN','SAREGAMA','SBFC','SBICARD','SBILIFE','SBIN','SCHAEFFLER','SCHNEIDER','SCI','SHREECEM','SHRIRAMFIN','SHYAMMETL','SIEMENS','SIGNATURE','SJVN','SOBHA','SOLARINDS','SONACOMS','SONATSOFTW','SPLPETRO','SRF','STARHEALTH','SUMICHEM','SUNDARMFIN','SUNPHARMA','SUNTV','SUPREMEIND','SUZLON','SWANCORP','SWIGGY','SYNGENE','SYRMA','TARIL','TATACAP','TATACHEM','TATACOMM','TATACONSUM','TATAELXSI','TATAINVEST','TATAPOWER','TATASTEEL','TATATECH','TBOTEK','TCS','TECHM','TECHNOE','TEGA','TEJASNET','TENNIND','THELEELA','THERMAX','TIINDIA','TIMKEN','TITAGARH','TITAN','TMCV','TMPV','TORNTPHARM','TORNTPOWER','TRAVELFOOD','TRENT','TRIDENT','TRITURBINE','TTML','TVSMOTOR','UBL','UCOBANK','ULTRACEMCO','UNIONBANK','UNITDSPR','UNOMINDA','UPL','URBANCO','USHAMART','UTIAMC','VBL','VEDL','VIJAYA','VMM','VOLTAS','VTL','WAAREEENER','WELCORP','WELSPUNLIV','WHIRLPOOL','WIPRO','WOCKPHARMA','YESBANK','ZEEL','ZENSARTECH','ZENTEC','ZFCVINDIA','ZYDUSLIFE','ZYDUSWELL']::text[]) AS symbol),
inst AS (SELECT u.symbol, i.id FROM uni u LEFT JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = u.symbol),
per AS (
  SELECT inst.symbol, inst.id,
         (SELECT min(c.ts) FROM ohlcv_candles c WHERE c.instrument_id = inst.id AND c.timeframe = '1d') AS first_1d,
         (SELECT max(c.ts) FROM ohlcv_candles c WHERE c.instrument_id = inst.id AND c.timeframe = '1d') AS last_1d,
         (SELECT min(c.ts) FROM ohlcv_candles c WHERE c.instrument_id = inst.id AND c.timeframe = '5m') AS first_5m,
         (SELECT min(c.ts) FROM ohlcv_candles c WHERE c.instrument_id = inst.id AND c.timeframe = '15m') AS first_15m
  FROM inst
)
SELECT count(*) AS universe, count(id) AS resolved_nse, count(id) - count(DISTINCT symbol) FILTER (WHERE id IS NOT NULL) AS duplicate_rows,
       count(first_1d) AS with_daily, min((first_1d AT TIME ZONE 'Asia/Kolkata')::date) AS earliest_daily,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY (first_1d AT TIME ZONE 'Asia/Kolkata')::date) AS median_first_daily,
       max((first_1d AT TIME ZONE 'Asia/Kolkata')::date) AS latest_first_daily, max((last_1d AT TIME ZONE 'Asia/Kolkata')::date) AS newest_daily,
       count(*) FILTER (WHERE last_1d < now() - interval '10 days') AS daily_stale_10d,
       count(first_5m) AS with_5m, percentile_disc(0.5) WITHIN GROUP (ORDER BY (first_5m AT TIME ZONE 'Asia/Kolkata')::date) AS median_first_5m,
       count(first_15m) AS with_15m, percentile_disc(0.5) WITHIN GROUP (ORDER BY (first_15m AT TIME ZONE 'Asia/Kolkata')::date) AS median_first_15m
FROM per;

-- RW3b. Universe stocks with daily history from each year start (how far back a backtest can rank them all)
WITH uni AS (SELECT unnest(ARRAY['360ONE','3MINDIA','AADHARHFC','AARTIIND','AAVAS','ABB','ABBOTINDIA','ABCAPITAL','ABDL','ABFRL','ABLBL','ABREL','ABSLAMC','ACC','ACE','ACMESOLAR','ACUTAAS','ADANIENSOL','ADANIENT','ADANIGREEN','ADANIPORTS','ADANIPOWER','AEGISLOG','AEGISVOPAK','AFCONS','AFFLE','AIAENG','AIIL','AJANTPHARM','ALKEM','AMBER','AMBUJACEM','ANANDRATHI','ANANTRAJ','ANGELONE','ANTHEM','ANURAS','APARINDS','APLAPOLLO','APOLLOHOSP','APOLLOTYRE','APTUS','ARE&M','ASAHIINDIA','ASHOKLEY','ASIANPAINT','ASTERDM','ASTRAL','ATGL','ATHERENERG','ATUL','AUBANK','AUROPHARMA','AWL','AXISBANK','BAJAJ-AUTO','BAJAJFINSV','BAJAJHFL','BAJAJHLDNG','BAJFINANCE','BALKRISIND','BALRAMCHIN','BANDHANBNK','BANKBARODA','BANKINDIA','BATAINDIA','BAYERCROP','BBTC','BDL','BEL','BELRISE','BEML','BERGEPAINT','BHARATFORG','BHARTIARTL','BHARTIHEXA','BHEL','BIKAJI','BIOCON','BLS','BLUEDART','BLUEJET','BLUESTARCO','BOSCHLTD','BPCL','BRIGADE','BRITANNIA','BSE','BSOFT','CAMS','CANBK','CANFINHOME','CANHLIFE','CAPLIPOINT','CARBORUNIV','CARTRADE','CASTROLIND','CCL','CDSL','CEATLTD','CEMPRO','CENTRALBK','CESC','CGCL','CGPOWER','CHALET','CHAMBLFERT','CHENNPETRO','CHOICEIN','CHOLAFIN','CHOLAHLDNG','CIEINDIA','CIPLA','CLEAN','COALINDIA','COCHINSHIP','COFORGE','COHANCE','COLPAL','CONCOR','CONCORDBIO','COROMANDEL','CPPLUS','CRAFTSMAN','CREDITACC','CRISIL','CROMPTON','CUB','CUMMINSIND','CYIENT','DABUR','DALBHARAT','DATAPATTNS','DCMSHRIRAM','DEEPAKFERT','DEEPAKNTR','DELHIVERY','DEVYANI','DIVISLAB','DIXON','DLF','DMART','DOMS','DRREDDY','ECLERX','EICHERMOT','EIDPARRY','EIHOTEL','ELECON','ELGIEQUIP','EMAMILTD','EMCURE','EMMVEE','ENDURANCE','ENGINERSIN','ENRIN','ERIS','ESCORTS','ETERNAL','EXIDEIND','FACT','FEDERALBNK','FINCABLES','FIRSTCRY','FIVESTAR','FLUOROCHEM','FORCEMOT','FORTIS','FSL','GABRIEL','GAIL','GALLANTT','GESHIP','GICRE','GILLETTE','GLAND','GLAXO','GLENMARK','GMDCLTD','GMRAIRPORT','GODFRYPHLP','GODIGIT','GODREJCP','GODREJIND','GODREJPROP','GPIL','GRANULES','GRAPHITE','GRASIM','GRAVITA','GROWW','GRSE','GVT&D','HAL','HAVELLS','HBLENGINE','HCLTECH','HDBFS','HDFCAMC','HDFCBANK','HDFCLIFE','HEGAM','HEROMOTOCO','HEXT','HFCL','HINDALCO','HINDCOPPER','HINDPETRO','HINDUNILVR','HINDZINC','HOMEFIRST','HONASA','HONAUT','HSCL','HUDCO','HYUNDAI','ICICIAMC','ICICIBANK','ICICIGI','ICICIPRULI','IDBI','IDEA','IDFCFIRSTB','IEX','IFCI','IGIL','IGL','IIFL','IKS','INDGN','INDHOTEL','INDIACEM','INDIAMART','INDIANB','INDIGO','INDUSINDBK','INDUSTOWER','INFY','INOXWIND','INTELLECT','IOB','IOC','IPCALAB','IRB','IRCON','IRCTC','IREDA','IRFC','ITC','ITCHOTELS','ITI','J&KBANK','JAINREC','JBMA','JINDALSAW','JINDALSTEL','JIOFIN','JKCEMENT','JKTYRE','JMFINANCIL','JPPOWER','JSL','JSWCEMENT','JSWDULUX','JSWENERGY','JSWINFRA','JSWSTEEL','JUBLFOOD','JUBLINGREA','JUBLPHARMA','JWL','JYOTICNC','KAJARIACER','KALYANKJIL','KARURVYSYA','KAYNES','KEC','KEI','KFINTECH','KIMS','KIRLOSENG','KOTAKBANK','KPIL','KPITTECH','KPRMILL','LALPATHLAB','LATENTVIEW','LAURUSLABS','LEMONTREE','LENSKART','LGEINDIA','LICHSGFIN','LICI','LINDEINDIA','LLOYDSME','LODHA','LT','LTF','LTFOODS','LTM','LTTS','LUPIN','M&M','M&MFIN','MAHABANK','MANAPPURAM','MANKIND','MAPMYINDIA','MARICO','MARUTI','MAXHEALTH','MAZDOCK','MCX','MEDANTA','MEESHO','MFSL','MGL','MINDACORP','MMTC','MOTHERSON','MOTILALOFS','MPHASIS','MRF','MRPL','MSUMI','MUTHOOTFIN','NAM-INDIA','NATCOPHARM','NATIONALUM','NAUKRI','NAVA','NAVINFLUOR','NBCC','NCC','NESTLEIND','NETWEB','NEULANDLAB','NEWGEN','NH','NHPC','NIACL','NIVABUPA','NLCINDIA','NMDC','NSLNISP','NTPC','NTPCGREEN','NUVAMA','NUVOCO','NYKAA','OBEROIRLTY','OFSS','OIL','OLAELEC','OLECTRA','ONESOURCE','ONGC','PAGEIND','PARADEEP','PATANJALI','PAYTM','PCBL','PERSISTENT','PETRONET','PFC','PFIZER','PFOCUS','PGEL','PHOENIXLTD','PIDILITIND','PIIND','PINELABS','PIRAMALFIN','PNB','PNBHOUSING','POLICYBZR','POLYCAB','POLYMED','POONAWALLA','POWERGRID','POWERINDIA','PPLPHARMA','PREMIERENE','PRESTIGE','PTCIL','PVRINOX','PWL','RADICO','RAILTEL','RAINBOW','RAMCOCEM','RBLBANK','RECLTD','REDINGTON','RELIANCE','RHIM','RITES','RKFORGE','RPOWER','RRKABEL','RVNL','SAGILITY','SAIL','SAILIFE','SAMMAANCAP','SAPPHIRE','SARDAEN','SAREGAMA','SBFC','SBICARD','SBILIFE','SBIN','SCHAEFFLER','SCHNEIDER','SCI','SHREECEM','SHRIRAMFIN','SHYAMMETL','SIEMENS','SIGNATURE','SJVN','SOBHA','SOLARINDS','SONACOMS','SONATSOFTW','SPLPETRO','SRF','STARHEALTH','SUMICHEM','SUNDARMFIN','SUNPHARMA','SUNTV','SUPREMEIND','SUZLON','SWANCORP','SWIGGY','SYNGENE','SYRMA','TARIL','TATACAP','TATACHEM','TATACOMM','TATACONSUM','TATAELXSI','TATAINVEST','TATAPOWER','TATASTEEL','TATATECH','TBOTEK','TCS','TECHM','TECHNOE','TEGA','TEJASNET','TENNIND','THELEELA','THERMAX','TIINDIA','TIMKEN','TITAGARH','TITAN','TMCV','TMPV','TORNTPHARM','TORNTPOWER','TRAVELFOOD','TRENT','TRIDENT','TRITURBINE','TTML','TVSMOTOR','UBL','UCOBANK','ULTRACEMCO','UNIONBANK','UNITDSPR','UNOMINDA','UPL','URBANCO','USHAMART','UTIAMC','VBL','VEDL','VIJAYA','VMM','VOLTAS','VTL','WAAREEENER','WELCORP','WELSPUNLIV','WHIRLPOOL','WIPRO','WOCKPHARMA','YESBANK','ZEEL','ZENSARTECH','ZENTEC','ZFCVINDIA','ZYDUSLIFE','ZYDUSWELL']::text[]) AS symbol),
per AS (
  SELECT (SELECT min(c.ts) FROM ohlcv_candles c WHERE c.instrument_id = i.id AND c.timeframe = '1d') AS first_1d
  FROM uni u JOIN instruments i ON i.exchange = 'NSE' AND i.symbol = u.symbol
)
SELECT y.from_day, count(*) FILTER (WHERE (first_1d AT TIME ZONE 'Asia/Kolkata')::date <= y.from_day - 140) AS ranked_from_then
FROM per, (VALUES (DATE '2022-01-03'), (DATE '2023-01-02'), (DATE '2024-01-01'), (DATE '2025-01-01'), (DATE '2026-01-01'), (DATE '2026-07-01')) y(from_day)
GROUP BY y.from_day ORDER BY y.from_day;

\echo
\echo '== RW4. RS Rotation Weekly''s latest saved version vs the repo file nifty_rs_rotation.py (354 lines): the line NUMBERS that differ (no code shown), and its settings'
SELECT v.version_number, array_length(string_to_array(replace(v.python_code, E'\r', ''), E'\n'), 1) AS saved_lines,
       (regexp_match(v.python_code, 'TOP_N = ([0-9]+)'))[1] AS top_n, (regexp_match(v.python_code, 'POSITION_SIZE_PCT = ([0-9.]+)'))[1] AS size_pct,
       (regexp_match(v.python_code, 'RS_WINDOW = ([0-9]+)'))[1] AS rs_window, (regexp_match(v.python_code, 'BENCHMARK_SYMBOL = "([^"]+)"'))[1] AS benchmark,
       array_length(regexp_split_to_array(substring(v.python_code from 'STOCK_UNIVERSE = \[(.*?)\]'), ','), 1) AS universe_commas
FROM strategies s JOIN LATERAL (SELECT * FROM strategy_versions v WHERE v.strategy_id = s.id ORDER BY v.version_number DESC LIMIT 1) v ON true
WHERE trim(s.name) = 'RS Rotation Weekly';

WITH code AS (
  SELECT v.python_code FROM strategies s JOIN LATERAL (SELECT * FROM strategy_versions v WHERE v.strategy_id = s.id ORDER BY v.version_number DESC LIMIT 1) v ON true
  WHERE trim(s.name) = 'RS Rotation Weekly' LIMIT 1
), saved AS (
  SELECT n AS line_no, md5(l) AS h FROM code, unnest(string_to_array(replace(code.python_code, E'\r', ''), E'\n')) WITH ORDINALITY AS u(l, n)
), repo AS (
  SELECT n AS line_no, h FROM unnest(ARRAY['d7ef7076734ae84447992034ead7723d','b077cdd7601ae17d976a8f2af04abc8a','23ccd91973fbce3346f63b0d980f8c3b','d41d8cd98f00b204e9800998ecf8427e','f17b33725017b6f284f104e36e13b715','2e20a8826ae4c8bfcd7a6d1f24476765','87f453567c3c5279863e8fd26f8ccd58','4cc711158567fbf6bbca2b4dbeac693f','d41d8cd98f00b204e9800998ecf8427e','be638e0c0bf77cc4c9391dc96bb3ffaa','85ff41c2d7581c50719f9e08e7c69184','d41d8cd98f00b204e9800998ecf8427e','853049e3822109320340571813105b24','6646fd2abb6a5133d50893bf7149bb27','ad112ecc779e5c2885f6c74503204835','4d8c15f4a8359d23a048fa6f53a2544f','7580e1cc37d71fa242b48cca589bdd06','a14b262826b205fd619d2b2411525219','9dd2254705422a462bd358cb756faf99','69cfd0197c60fdf78ca035780586d497','4d9d22143df8c55fce4fbbe65f7650ad','d41d8cd98f00b204e9800998ecf8427e','247ef6d070d42efb21c04852e1c19b35','70d1a391a2987831a663e584f76c3f6a','b955251aa22af0f6dc64026dc04f1aca','07888ea3037542f0d060354ad4b795be','00119b8ce77e0d221f83b12d74b06fa4','3bc7fa3f69f4024cdda924b554ec9c2f','5a3e7dd559e32f957290204b43bb4b02','a2414926dc6921edfbd9fcb0ed680a32','2f418fd8e7d1de34bb6b5f30b42cd495','7e660e906c7b25c636e8cc67ff8ca8d5','37884a69a1d0e758e7799db5df767a02','d41d8cd98f00b204e9800998ecf8427e','76b4ee0fc41239c37a2d25a25f138d07','e6b48fa4d6b9765746bb6534ef84a1d2','3f26bfd25915787c49948019b6cd502f','a8fe8fa36b224e5a28756723116d1f00','c394c3a6da408759740578f1e89a6f79','09954fcae2b10ba611f3d499354c0706','b61dbbf9dbadc3004e6876b111edbca7','b5e27533f0bf636a3a650691499a4139','7c72994802918724b2c0102d6fe1a274','72a3baf9fea27135c3b93bb8398ea147','aafef64e288a5a30528609eea60c93f1','eeae114271e826870603acd963195362','d41d8cd98f00b204e9800998ecf8427e','3d6d518f9cd053a5505f663a9bd53cee','b1965bffc8dae79afe13b82dfd0e4c03','aeda06fd18f3d0dbbdcf5db18bae0398','951673c5ea5357c504fc3dd76164254c','50ba653ea287ddfe8dbbbb20454205ae','151ba79911fd47a1457ffac11fb50dcf','e77b50b0ca1f7d1efdcca249e64f257b','33c446676452f08c40d343df6d0457a5','9f35fd3b614784d00507ed2aa8bce6f3','d41d8cd98f00b204e9800998ecf8427e','ea8b3bade4ab122758f3619fcf2f9944','0497b578d15ae4c0a2652467702f127f','f3070877759663bc8d3f754e33b3ff74','ebe87aaf16e92a946d07c1b3f4606cec','694787db9027e8901d3aae163d84613c','d41d8cd98f00b204e9800998ecf8427e','4e1e852f5a10042eee885dc5c76e2352','29c0f55b184cff2abcb5db766fd0014c','1343371781b523f784e82feebef5fbff','ea8e5722a25f4344f9d9d301bf603b54','92daf5b1bd341c4c69a2e96d7ff2fccd','d774f3b10f9c3ff0b0fbbee87db85c83','326f7bf0f357f00a6e8d7f9045254fe8','f211edbae33b8ca4a929c2b208430678','16b6e1ba640151bbcfc38bb357976b56','886561fd4a80f5aa1291ddd1578edcc8','d41d8cd98f00b204e9800998ecf8427e','8e6ffc31f762073de8e6fb2bc01078aa','d41d8cd98f00b204e9800998ecf8427e','fd2ee1aecc6cd30c93bc93c00c81846b','0cffe17503b182d1ade8ef97200d1bf8','a614b2f9cfafec8d43a7a51001d1b50a','e3c0be02ce775080d7b8ad1bc548566b','d7ef7076734ae84447992034ead7723d','d41d8cd98f00b204e9800998ecf8427e','0f1b5a0177517360bb269df711667a96','aac5d1e6fc13540fb4772d8e9173c641','d41d8cd98f00b204e9800998ecf8427e','074f69dfe6ef6f55891bb04bbeb4dd0a','d41d8cd98f00b204e9800998ecf8427e','46f02925550cb8cfce84336a85050563','1364992b644f51aef417dfe09b159263','a1313ca803bc0479e5fed4cb135b9d2b','f6ca10d3cb1d0fc88bc96d8f738602f5','4a648cf60d7b6033813bc77b9ce701cb','d41d8cd98f00b204e9800998ecf8427e','6829c159f39367f51b7eb5b0f103dee8','3a021112a1453ae4a832597be504b05a','ef4f087f032a94e4c01d7ff984287256','9fad999725dca27855aa3265e57dfe6b','637b0e09ce3c2289ca17fabc86200b07','91554377ff526f1907ba3e037d4cacdb','ebd9f27f111216fa7591a25db9010898','72d35b5056f87881696a99cd5716caa1','bb39f2acd5ab3ba8a740d2f92384362d','d2dc5948302d1983f73016c8e48e0f45','920acb89e085786277a72344ebf333e6','3108a140e40c3fda5241d2d54d894ce9','cded759bc76a7e859fa727510eca0794','2096e71fd0161494ae7486eca17b1592','0b61874ee7f89852be8ec4d922c01414','95d4930e3cbda5d66ede99e122481044','e3d091fdfc23d8fc560f60f7783c923f','0ac457ded3787f038687740ca0ece02d','f5ce53891430d4120b0f2317ef62f32a','cfdfd592beb22d85a513c68b807ee6b8','ffebf7ffeadefd63dd739fe0bd614303','d7f5971ce8d366d999da2e6140313b56','934ce1fe21860d9d6d4d96c53e990fe9','8d9bd963779e5e4d7591108455067bd0','85e530a54582c6cd0fb7fb6068018e36','086aaf694db20e6740f9a3b1834a93ae','61b72b9f3c9d590c619ea5fb8b0c3d06','b8a4c2ae90fb4f51e804d393177de3d6','e3e6b4efbd4955f6dbc7a2c0af8f6c50','1759e0ff516fbb608b620b144710cc7c','8c8f15c5694e216d5aeeff8d95396c90','797cb88b6202d68ffcc3e017a668482e','f881d44c7399b4929af2eb4c879e80a6','a64feac82364c6fc08b931ddd1d510bb','792be6253d787f3919be6834ebc8fbdf','d39b9fab21b7b83ff1f31d48dcbbba1e','f84f52b5b3ffe283e0d6e4633ed3f223','ed7df12ee257a0cad959bc5f23e33900','56131ecf69ac0cdf699845c03422762a','1ccea2e7ea6687ad3eefebd1734639e0','c953d25c9f6cfa24880de9dc9e923cbe','b6a0ce4e6f11fda8bb47d401297bb741','642013a35aec3c04100fb8fe8505f4d6','b0efc0f45959b5d813717b033dfb4e06','8abb995e547d4a14165494360c7c9e45','e07e128ca718ecad58a82de38e534273','219290a4c51e0cc4581323d528c0f3c0','eafc07bbfc8d56db5adb19e1adaaa771','13c32a55915838551a13e0852be98acf','22952a35430f58a8d9e6c034690e5889','0a346e5059ffd072386787e03f4c6311','6aa57bcb412b8a602fef86fae0a35ff9','4755abf7668f47fa1574cf595061daa3','a03efba403603624cf35545d8295c346','64c2237bde1835e62a21c8d22eff2d67','362815252bff59b683466b7b34465c5d','e9656b8f51d349a759b4dbcb17f7a4b5','ae7f72ca9c1941e6cb6f97b57e944fa2','a2e32c2025e134944562f1fe2cdb3e22','fbd2c6f2da84aaa46ed39e91312cbf31','184f37b1c15a21da2fcd2133acc197ac','817720d5d14b659ac592bee9d6270886','f947035c8020de25b94300a7849fb937','4782d332bb95f5c3f64236012e3eda41','9b446447bbe2d2f5e8bd10cfacef50ff','0fbd1776e1ad22c59a7080d35c7fd4db','c099636ed470b1e03e14f0f38c83d2bd','d41d8cd98f00b204e9800998ecf8427e','6172c97bcd45f0189f68a9cb240a6858','d41d8cd98f00b204e9800998ecf8427e','0b16068418bcc0bb48e2af25740d88da','e4e2ef8dea043640fc21ccb9526531b4','d41d8cd98f00b204e9800998ecf8427e','34a78dbf05e0ff336c3c3e7781905026','0fc1e7c69e26ab9c9a65f453fc3f31bb','e7c62712c0806dfe777f9df039195d27','b7f729914e63e84941412799830b1160','d41d8cd98f00b204e9800998ecf8427e','95d65433022100263e347f84ca91a54b','3da8e08f40265ffd8d90a435dae10b9c','9abf9449d7b632c46a4522801b51e997','60db6d57be5e73b80cfa8281c94b57a7','025a69e679016c0b8f14d972b342d64b','2e0342bb4ea3ec34c09eff1021f61907','d41d8cd98f00b204e9800998ecf8427e','a35d3cc66ef4e7d253cf2254ff11c198','276eda85f7a9978075f8263148b98004','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','69a49900b69b534654d06c59e48b35e7','04d298f1d0e15e8f64ddeea02610d521','a117d222f5da88aa0df189303e830101','a3d2e92fbd84ad4093991aa7c53e7c88','a50120511be0e9146ec58329a967e960','779eae9ab4fdb961a7d45e1bee01b915','3711606fb145f16418595cc09af5837f','4c15bec244dd50a8dbb7106d4d8f1365','161ad08f1602b0a43f09d7658b193287','7625180ed9d372ef540625b2f033e546','04d209e014b5a8598a88471f9d3ad777','df32458188aa952cebd532afbfc60734','be7b54d022d3e9910af93cc5da3d9f4f','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','bdb00ef75c821d252e5e876a8912c3f3','45fdecb78b5db9fa8b8415f1669aaa0e','694edb26ac194061aa263d6de676acdf','a7f9ef6d5c0fbe8be21d756e1740d6ad','d81e8dd67f50d7e8749876a7d3718adb','87ec711d61ac2acd54c7386f7bcada2f','5c398a6968507454ba18432a85a83f63','3ff28126c2cfb25a6217f9f1590468a9','13a0d9b213042fc7cd1f7a38ae6a02d3','996d4b7f0830ff4ad3ad62d5e361c7ca','6b3c75df8a65413ee7b6c8b82da48af5','25f0be2e2538f1d0a70adc155aa649db','b32559289d76659c2e7ae894a32e91ef','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','5677800f1070685f0185ef0d7506dc8f','d362c6227a4ece99fe4e18c19379346e','714fa39798b82eeb008eff6c1b58e4f5','6a991042d8a213824eb83201bcb3948d','c4aa914095c218155ab79c290bde03c4','ddb3fc282f81bfb9fe2cb5f8d375ab32','de8df2a8fdd06f152bf78596269caef0','6a991042d8a213824eb83201bcb3948d','e4632a9f9a55e4f313806d2a2cbbbf7f','49880d29daa31ae8f1d24a6f37f81836','4614066372a2a4a2863092b83d5fae43','6a991042d8a213824eb83201bcb3948d','4094e9e5a959268f2b67c0f5284c9781','d41d8cd98f00b204e9800998ecf8427e','d41d8cd98f00b204e9800998ecf8427e','a7f46a27f8a432e99d29de92ff7cf0f7','92bfdd956046f977838e35d530e39c3f','a253439691fea53d5c9aab20c9981f7c','1e7914067bf06c2b254e597d4145bad9','bfcdd9c0bc8c97debe6e76af8b6a376f','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','35c9d72414ff373107b52a726f6d882d','c467efc0f4b377d3df8a0053348f4ffb','ceeebccbf4f54ed3566f5f76c7ef3ce7','78f395d5b3ec338e2096e424263c6e32','03ffe1d5aa6d53e56e56ab64e8d87958','e8e17fc38e4152f9f8157fe8e92ddc09','c13a298558bd7494527f16d0b47c97f9','415ece355bdeb6b5dc29971def54613c','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','2ea31616bea4b23927272040cbb6b507','798a62f0581c5c869bd1b0fb5460deb8','e5efc22167c6ddcbc6885954b9f39b5a','c7edca3bf34b2d4e98f338b492e82bb5','ec8a2c195dc5ade16feb75cb2c75d310','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','9bab216f7c4c279a083c543457c70b32','ff9c8db95b543196e1d4921d714a5421','5c398a6968507454ba18432a85a83f63','ee035a2d827d34afcaf44cea6dd4aadd','6bca2a6982d954da9cf60dd84ae3c3d2','1c9ce33726250bf8467d31889c20a0d5','288c32015c78b6ef497bfb770c9919f7','356d11f641029bf139d156c7e3c838fb','b2f2e01dc8564b663bbfef863325ff1e','f133f2aa67f2adcfb9f4d2de55f37e3b','4b87562299225b28a3a4d3d8119f751d','d41d8cd98f00b204e9800998ecf8427e','e6c3afae792abafe8e3c88d806ef7068','f91e7cea05289b94ffd90c1b7918ddb1','5d9106c8bb87bea4e028d08b06645e90','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','5d476d5108ab95c2c17c0412e572f2b6','3610f9412f23b582f531a0c89f022693','d63fce03cae047503effe48ef82d4a6c','f87e18546114320f4826217cb480d1bf','a80cfb424b9367064c3b951341e7dda7','891ee754b8ed394ac5d5d4acf6596e41','d41d8cd98f00b204e9800998ecf8427e','82ea583d26a8d3ed8a4ff3dded744f14','aa66b7fa7cd31c02f95e5db9bcfd1757','b4eb7b35a17aa583578640214a709099','97a4e3b581ea70dc86102502736955e7','d41d8cd98f00b204e9800998ecf8427e','33c6c43134bce655e600254614aa54d6','3950924399af315ca5dc6c5ac96f7dc6','d41d8cd98f00b204e9800998ecf8427e','3acd0f218d7e6e95a87f235670d4f168','d41d8cd98f00b204e9800998ecf8427e','37bf11dfeaa32995b0251dfbbabdc16e','4e8ee35ffa73c9830978ce3f173dc243','0f62de805c09b89a8873a73931df8398','c47179effae6b4184c916cb1674b6af8','f202b204490851c52771eb0465eab797','029e131d2a673b9be515fd3c31e8ed98','ca90ae2b4104037a5bc4bb793520e77e','b0ebed39f93be536c0b538248227061e','a2e3428ec5f7558213b9daa2af0485d3','1eb51725a72381c8dc1febfe8d1e2854','129ddcfd7c199e34c4eb2601cf54f174','a5b936c0343c236c2a5534f0e668c031','b0ebed39f93be536c0b538248227061e','88c6e9e9654190aa0ecf817ac0c08fda','e9d0f73325781c74de36bf01aac15ff0','b2dc75d7315adc4deffa741a07c207e3','92d0588ef5ba066106a447826f28f1b4','1698ce72d9c6b44097e8828d87b68244','717a6141fd1b3936576f6eea01007c2c','7a356ddfaceca5abf00071d33c808ed7','da7e226902f73fbc881f8d2b08d57057','19062f1990be7c26cc6adfd553a42c00','8b89d8759f4cf042ae3068df22419d9e','e43aab9aa6639d4f26a2619eef5d9b8c','182f7238408f13ef71d766a8a0a33dc8','fc5fe91398c59b53b8a8c501192eeca9','045399dcce6b7a4d9a7387758c4d49e0','415ece355bdeb6b5dc29971def54613c','7a94ef9c38e9d259ef5da49e2b24089e','d41d8cd98f00b204e9800998ecf8427e','2c208499a1cb41a5643d1cf70845ab62','5d2ac1f76d1e7badbb7ca7a99923ca7b','ab3a6b21e42444b965c649ea6ebe8a67','5f2a61fdfebe84466acb7ad905cb3be3','742d1dd9271968d0530ff5b5ff94b19f','9870a014f8bc2bcae8bd70e8b929d66f','d41d8cd98f00b204e9800998ecf8427e','8cee85e440f8b5108fac95eb6f1be76d','760a6aacc16eee20616ae4d6dd167368','fc8755590d59799d5c0956d32939b9f8','129ddcfd7c199e34c4eb2601cf54f174','b0ebed39f93be536c0b538248227061e','e9d0f73325781c74de36bf01aac15ff0','b2dc75d7315adc4deffa741a07c207e3','b0ebed39f93be536c0b538248227061e','ec8d54aacf81f63238d0c8209504cc4f','e3cd4120d01de28a43dc23ced30ace57','8091ad46babd30e3a0263d0251394ea9','fe1ceb7bd7808f4ea6235598960d2cfa','b0ebed39f93be536c0b538248227061e','1d7f8c300ede3f612397f7786e85da09','e6cb051e0e122db816ab2255efe5a07b','9823881a641c3723885400fc8379bc9e','c14ceb01f5fc45acabada148c2f6ad62','4e1ddba1c31545ee09fb38ae1fd79346','f8479fd1cd59f310028d27a221b85079','9b3196a9220b8925c5315e434429d414','d41d8cd98f00b204e9800998ecf8427e','79f3d8d1df040394645726f247397fe8','6a3fdf48cd93deb3896e371aaf1d212b','d41d8cd98f00b204e9800998ecf8427e','dc7f11c9605b11b0d88c1857c9acee1e','2b26927a349d9d1c80769b626cab8179','60034d8b1e7fd74a2bf6a920f0819ae1','243365040effbf0ec8f5750ccf0a1541','047370f54a73719b1c080f3a05222816','efdba9d12f3433066bf3713391969d07','61d8ebf2eeb23a0115509cf7b6be468e','d41d8cd98f00b204e9800998ecf8427e']::text[]) WITH ORDINALITY AS r(h, n)
)
SELECT 'only in saved' AS which, string_agg(line_no::text, ',' ORDER BY line_no) AS line_numbers, count(*) AS lines FROM saved WHERE h NOT IN (SELECT h FROM repo)
UNION ALL
SELECT 'only in repo', string_agg(line_no::text, ',' ORDER BY line_no), count(*) FROM repo WHERE h NOT IN (SELECT h FROM saved);

-- RW5. Its latest version's fingerprint ignoring blank lines and trailing spaces (migration 8f2b4d6a1c35 saves v4 only when it is ff02ad89...)
SELECT v.version_number,
       md5(array_to_string(ARRAY(
         SELECT rtrim(l, E' \t') FROM unnest(string_to_array(replace(v.python_code, E'\r', ''), E'\n')) WITH ORDINALITY u(l, n)
         WHERE rtrim(l, E' \t') <> '' ORDER BY n), E'\n')) AS norm_md5
FROM strategies s JOIN LATERAL (SELECT * FROM strategy_versions v WHERE v.strategy_id = s.id ORDER BY v.version_number DESC LIMIT 1) v ON true
WHERE trim(s.name) = 'RS Rotation Weekly';

\echo
\echo '== RW6. RS Rotation Weekly''s newest backtest: its trades by kind and the days it traded (counts and dates only -- no P&L; see the Backtests page)'
WITH job AS (
  SELECT b.* FROM native_backtest_jobs b JOIN strategies s ON s.id = b.strategy_id
  WHERE trim(s.name) = 'RS Rotation Weekly' ORDER BY b.created_at DESC LIMIT 1
)
SELECT v.version_number AS ran_version, job.start_date, job.end_date, job.status,
       round(extract(epoch FROM (coalesce(job.completed_at, now()) - job.started_at)) / 60.0, 1) AS run_minutes,
       count(t.id) AS trades,
       count(t.id) FILTER (WHERE t.exit_reason = 'dropped_out_of_top_n') AS sold_out_of_top10,
       count(t.id) FILTER (WHERE t.exit_reason = 'trimmed_to_equal_weight') AS trims,
       count(t.id) FILTER (WHERE t.exit_reason = 'backtest_end') AS closed_at_end,
       count(t.id) FILTER (WHERE t.exit_reason NOT IN ('dropped_out_of_top_n', 'trimmed_to_equal_weight', 'backtest_end')) AS other,
       to_char(min(t.opened_at) AT TIME ZONE 'Asia/Kolkata', 'Dy DD Mon HH24:MI') AS first_buy_ist
FROM job JOIN strategy_versions v ON v.id = job.strategy_version_id LEFT JOIN native_backtest_trades t ON t.job_id = job.id
GROUP BY v.version_number, job.start_date, job.end_date, job.status, job.completed_at, job.started_at;

-- RW6b. When it readjusted: trades closed per weekday and time (IST) -- Fridays 15:00, a Thursday before a Friday holiday
WITH job AS (
  SELECT b.id FROM native_backtest_jobs b JOIN strategies s ON s.id = b.strategy_id
  WHERE trim(s.name) = 'RS Rotation Weekly' ORDER BY b.created_at DESC LIMIT 1
)
SELECT to_char(t.closed_at AT TIME ZONE 'Asia/Kolkata', 'Dy HH24:MI') AS closed_at_ist, count(*) AS trades,
       count(DISTINCT (t.closed_at AT TIME ZONE 'Asia/Kolkata')::date) AS days,
       string_agg(DISTINCT to_char(t.closed_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon'), ', ')
         FILTER (WHERE extract(isodow FROM t.closed_at AT TIME ZONE 'Asia/Kolkata') <> 5) AS non_friday_days
FROM native_backtest_trades t JOIN job ON job.id = t.job_id
GROUP BY 1 ORDER BY 2 DESC;

\echo
\echo '== AU1. Sessions today (IST) by user (numbered) and device/browser kind: fresh logins vs refresh renewals, still active, distinct networks (counts only, no IPs)'
WITH u AS (SELECT id, 'User ' || row_number() OVER (ORDER BY created_at) AS label FROM users),
s AS (
  SELECT s.*, u.label,
         CASE WHEN s.user_agent ~* 'iphone' THEN 'iPhone' WHEN s.user_agent ~* 'ipad' THEN 'iPad' WHEN s.user_agent ~* 'android' THEN 'Android'
              WHEN s.user_agent ~* 'windows' THEN 'Windows' WHEN s.user_agent ~* 'macintosh' THEN 'Mac' WHEN s.user_agent ~* 'linux' THEN 'Linux' ELSE 'other' END
         || ' ' ||
         CASE WHEN s.user_agent ~* 'telegram|fban|fbav|instagram|whatsapp|; wv\)' THEN 'in-app browser'
              WHEN s.user_agent ~* 'edg/' THEN 'Edge' WHEN s.user_agent ~* 'opr/' THEN 'Opera' WHEN s.user_agent ~* 'crios|chrome/' THEN 'Chrome'
              WHEN s.user_agent ~* 'fxios|firefox/' THEN 'Firefox' WHEN s.user_agent ~* 'safari/' THEN 'Safari' ELSE 'other' END AS device,
         EXISTS (SELECT 1 FROM sessions p WHERE p.user_id = s.user_id AND p.revoked_at BETWEEN s.created_at - interval '2 seconds' AND s.created_at + interval '2 seconds'
                 AND p.id <> s.id AND p.created_at < s.created_at) AS renewal
  FROM sessions s JOIN u ON u.id = s.user_id
  WHERE s.created_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')
)
SELECT label AS user_no, device, count(*) AS sessions, count(*) FILTER (WHERE NOT renewal) AS fresh_logins, count(*) FILTER (WHERE renewal) AS renewals,
       count(*) FILTER (WHERE revoked_at IS NULL AND expires_at > now()) AS active_now, count(DISTINCT ip_address) AS networks
FROM s GROUP BY label, device ORDER BY label, device;

-- AU2. Today's fresh logins in order (IST time, user number, device/browser kind) -- no IPs
WITH u AS (SELECT id, 'User ' || row_number() OVER (ORDER BY created_at) AS label FROM users),
s AS (
  SELECT s.created_at, u.label,
         CASE WHEN s.user_agent ~* 'iphone' THEN 'iPhone' WHEN s.user_agent ~* 'ipad' THEN 'iPad' WHEN s.user_agent ~* 'android' THEN 'Android'
              WHEN s.user_agent ~* 'windows' THEN 'Windows' WHEN s.user_agent ~* 'macintosh' THEN 'Mac' WHEN s.user_agent ~* 'linux' THEN 'Linux' ELSE 'other' END
         || ' ' ||
         CASE WHEN s.user_agent ~* 'telegram|fban|fbav|instagram|whatsapp|; wv\)' THEN 'in-app browser'
              WHEN s.user_agent ~* 'edg/' THEN 'Edge' WHEN s.user_agent ~* 'opr/' THEN 'Opera' WHEN s.user_agent ~* 'crios|chrome/' THEN 'Chrome'
              WHEN s.user_agent ~* 'fxios|firefox/' THEN 'Firefox' WHEN s.user_agent ~* 'safari/' THEN 'Safari' ELSE 'other' END AS device,
         EXISTS (SELECT 1 FROM sessions p WHERE p.user_id = s.user_id AND p.revoked_at BETWEEN s.created_at - interval '2 seconds' AND s.created_at + interval '2 seconds'
                 AND p.id <> s.id AND p.created_at < s.created_at) AS renewal,
         s.revoked_at, (SELECT count(*) FROM sessions c WHERE c.user_id = s.user_id AND c.created_at > s.created_at
                        AND c.created_at <= coalesce(s.revoked_at, now()) + interval '2 seconds' AND c.created_at >= coalesce(s.revoked_at, now()) - interval '2 seconds') AS renewed_into
  FROM sessions s JOIN u ON u.id = s.user_id
  WHERE s.created_at >= (date_trunc('day', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata')
)
SELECT to_char(created_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS created_ist, label AS user_no, device,
       CASE WHEN renewal THEN 'renewal' ELSE 'LOGIN' END AS kind,
       to_char(revoked_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI:SS') AS revoked_ist, renewed_into
FROM s ORDER BY created_at;

\echo
\echo '== UA1. NSE stocks still marked "unassigned" (no live source) that an active strategy names -- numbered, no names: on the backfill list?, bars there, chart candles, last copied, which strategy'
WITH active_code AS (
  SELECT DISTINCT s.name AS strategy, v.python_code
  FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id JOIN strategy_versions v ON v.id = d.strategy_version_id
  WHERE d.status IN ('active', 'paused')
), ua AS (
  SELECT i.id, i.symbol, i.instrument_type, i.is_active,
         (SELECT string_agg(DISTINCT a.strategy, ', ') FROM active_code a
           WHERE a.python_code LIKE '%"' || i.symbol || '"%' OR a.python_code LIKE '%''' || i.symbol || '''%') AS used_by
  FROM instruments i WHERE i.exchange = 'NSE' AND i.data_source = 'unassigned'
)
SELECT 'Stock ' || row_number() OVER (ORDER BY ua.symbol) AS stock, ua.instrument_type, ua.is_active, ua.used_by,
       bf.id IS NOT NULL AS on_backfill_list, to_char(bf.last_synced_at AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') AS last_copied_ist,
       (SELECT count(*) FROM bf_ohlcv_bars b WHERE b.symbol_id = bf.id AND b.timeframe = '15m') AS bf_15m,
       (SELECT count(*) FROM bf_ohlcv_bars b WHERE b.symbol_id = bf.id AND b.timeframe = '1d') AS bf_1d,
       (SELECT count(*) FROM ohlcv_candles c WHERE c.instrument_id = ua.id AND c.timeframe = '15m') AS chart_15m,
       (SELECT to_char(max(c.ts) AT TIME ZONE 'Asia/Kolkata', 'DD Mon HH24:MI') FROM ohlcv_candles c WHERE c.instrument_id = ua.id AND c.timeframe = '15m') AS chart_15m_last,
       (SELECT count(*) FROM bf_symbols x WHERE x.source = 'zerodha' AND x.symbol LIKE ua.symbol || '-%') AS bf_suffix_variants
FROM ua LEFT JOIN bf_symbols bf ON bf.source = 'zerodha' AND bf.symbol = ua.symbol
WHERE ua.used_by IS NOT NULL
ORDER BY stock;

-- UA2. All NSE catalog rows by data source (counts)
SELECT data_source, instrument_type, count(*) AS rows, count(*) FILTER (WHERE is_active) AS active
FROM instruments WHERE exchange = 'NSE' GROUP BY 1, 2 ORDER BY 1, 2;

\echo
\echo '== PG1. NIFTY PCR per session since its records began (market data): the PCR filter of RS Rotation 15 MIN / MACD - RSI - 15 MIN sells all below 0.80, buys again above 0.90'
SELECT session_date, count(*) AS records, round(min(pcr)::numeric, 3) AS min_pcr, round(max(pcr)::numeric, 3) AS max_pcr,
       round((array_agg(pcr ORDER BY ts))[1]::numeric, 3) AS first_pcr, round((array_agg(pcr ORDER BY ts DESC))[1]::numeric, 3) AS last_pcr,
       count(*) FILTER (WHERE pcr < 0.80) AS marks_below_080, count(*) FILTER (WHERE pcr > 0.90) AS marks_above_090,
       count(*) FILTER (WHERE contracts_expected = 0 OR contracts_with_oi < 0.9 * contracts_expected) AS low_coverage
FROM pcr_snapshots WHERE underlying = 'NIFTY' AND pcr IS NOT NULL
GROUP BY session_date ORDER BY session_date;

-- PG2. Runs of the two 15-minute cash strategies by code version (counts only): before the filter (561759bb / 98824676) or with it (c5d7e9f1a3b4)
SELECT trim(s.name) AS strategy, d.status, v.version_number,
       CASE md5(replace(v.python_code, E'\r', ''))
         WHEN '561759bbee3e25378f16c524c7f2f07b' THEN 'MACD before PCR filter' WHEN '988246769bebae7d1d8c3cdfbcf91be8' THEN 'RS 15 before PCR filter'
         WHEN '5f43c1d04e769861328c0e7b54ff2729' THEN 'MACD with PCR filter' WHEN '92c7c0d9723cb8962692b9e39bb0166b' THEN 'RS 15 with PCR filter'
         ELSE 'other code' END AS code,
       (SELECT count(*) FROM live_native_deployments l WHERE l.paper_deployment_id = d.id AND l.status IN ('active', 'paused')) AS live_runs_on,
       (SELECT count(*) FROM live_native_deployments l WHERE l.paper_deployment_id = d.id AND l.status IN ('active', 'paused')
          AND l.strategy_version_id = d.strategy_version_id) AS live_on_same_version,
       (d.state ->> 'pcr_risk_off') AS out_on_pcr
FROM paper_native_deployments d JOIN strategies s ON s.id = d.strategy_id JOIN strategy_versions v ON v.id = d.strategy_version_id
WHERE trim(s.name) IN ('RS Rotation 15 MIN', 'MACD - RSI - 15 MIN') AND d.status IN ('active', 'paused')
ORDER BY 1, 3;
