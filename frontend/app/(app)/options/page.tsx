"use client";

import { useMemo, useState } from "react";

import type { OverlayLine } from "@/components/charts/price-chart";
import { OscillatorChart } from "@/components/charts/oscillator-chart";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Select } from "@/components/ui/select";
import { ConnectionStatusBadge } from "@/components/ui/status-badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { useOptionChain, useOptionExpiries, useOptionHistoryDepth, useOptionPcr, useOptionUnderlyings } from "@/lib/hooks";
import type { OptionLegOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMarketDataSocket } from "@/lib/ws";

const PCR_TIMEFRAMES = ["5m", "15m", "1h", "1d"];

type Moneyness = "ITM" | "ATM" | "OTM";
type MergedLeg = OptionLegOut;

// ITM shaded green on the call side / red on the put side (mirrors the OI
// bars' own positive/negative coloring below), ATM shaded blue regardless
// of side, OTM left unshaded -- plus a text "ATM" tag on the strike itself
// (PRD 39.3: never rely on color alone), since that's the one row where
// misreading the shading actually matters.
const MONEYNESS_BG: Record<"call" | "put", Record<Moneyness, string>> = {
  call: { ITM: "bg-positive-soft", ATM: "bg-active-soft", OTM: "" },
  put: { ITM: "bg-negative-soft", ATM: "bg-active-soft", OTM: "" },
};

function classifyStrike(strike: number, atmStrike: number | null, side: "call" | "put"): Moneyness | null {
  if (atmStrike == null) return null;
  if (strike === atmStrike) return "ATM";
  if (side === "call") return strike < atmStrike ? "ITM" : "OTM";
  return strike > atmStrike ? "ITM" : "OTM";
}

function fmt(n: number | null | undefined, digits = 2): string {
  return n == null ? "--" : n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function ChangeCell({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-text-muted">--</span>;
  return (
    <span className={value >= 0 ? "text-positive" : "text-negative"}>
      {value >= 0 ? "+" : ""}
      {fmt(value, 0)}
    </span>
  );
}

/** A leg's live-merged view: the snapshot's day-open baseline (ltp -
 * ltp_change / oi - oi_change) stays fixed for the page's lifetime, while
 * ltp/oi themselves are overridden by a live WS tick as soon as one
 * arrives -- so "change" tracks the live value against the same baseline
 * rather than freezing at the last poll. A plain function (not a hook) so
 * the whole table can be merged once at the page level -- ATM detection
 * below needs every row's merged ltp before any row renders. */
function mergeLeg(leg: OptionLegOut | null | undefined, live: { price: number; open_interest: number | null } | undefined): MergedLeg | null {
  if (!leg) return null;
  const dayOpenLtp = leg.ltp_change == null || leg.ltp == null ? null : leg.ltp - leg.ltp_change;
  const dayOpenOi = leg.open_interest_change == null || leg.open_interest == null ? null : leg.open_interest - leg.open_interest_change;
  const ltp = live?.price ?? leg.ltp;
  const oi = live?.open_interest ?? leg.open_interest;
  return {
    ...leg,
    ltp,
    ltp_change: ltp != null && dayOpenLtp != null ? ltp - dayOpenLtp : leg.ltp_change,
    open_interest: oi,
    open_interest_change: oi != null && dayOpenOi != null ? oi - dayOpenOi : leg.open_interest_change,
  };
}

function fmtDate(ts: string | null): string {
  return ts ? new Date(ts).toLocaleDateString() : "--";
}

/** Answers "how much historical data is really there" by asking Kite's
 * own API directly, live, through the already-connected Zerodha session --
 * not just what this app happens to have backfilled (our_*). Not
 * auto-fetched (see useOptionHistoryDepth): it's a live broker API call,
 * so it only runs when explicitly requested. */
function HistoryDepthCard({ underlyingId, expiry }: { underlyingId: string; expiry: string }) {
  const { data, isFetching, isError, refetch, isFetched } = useOptionHistoryDepth(underlyingId || null, expiry || null);

  return (
    <Card>
      <CardHeader className="flex-wrap gap-3">
        <CardTitle>Data Coverage</CardTitle>
        <Button size="sm" variant="secondary" onClick={() => refetch()} disabled={!underlyingId || !expiry || isFetching}>
          {isFetching ? "Checking..." : "Check Kite's real depth"}
        </Button>
      </CardHeader>
      <CardContent>
        {!isFetched && !isFetching ? (
          <p className="text-sm text-text-muted">
            An option contract only trades from its own listing date to its own expiry -- typically a few weeks to a
            few months, far less than an equity or index&apos;s history. Click the button to ask Kite&apos;s API directly,
            live, how much data it actually has for this expiry (through the connected Zerodha session), alongside
            what this app has already backfilled.
          </p>
        ) : isError ? (
          <ErrorState description="Could not run the history-depth check." />
        ) : data ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-text-muted">Backfilled in this app</div>
              <div className="text-sm text-text-primary">
                {data.our_candle_count} candles
                {data.our_earliest && data.our_latest && (
                  <span className="text-text-secondary"> ({fmtDate(data.our_earliest)} -- {fmtDate(data.our_latest)})</span>
                )}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-text-muted">Kite&apos;s live API (right now)</div>
              {data.error ? (
                <div className="text-sm text-text-muted">{data.error}</div>
              ) : (
                <div className="text-sm text-text-primary">
                  {data.kite_candle_count} candles
                  {data.kite_earliest && data.kite_latest && (
                    <span className="text-text-secondary"> ({fmtDate(data.kite_earliest)} -- {fmtDate(data.kite_latest)})</span>
                  )}
                </div>
              )}
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ChainRow({
  strike,
  call,
  put,
  maxOi,
  atmStrike,
}: {
  strike: number;
  call: MergedLeg | null;
  put: MergedLeg | null;
  maxOi: number;
  atmStrike: number | null;
}) {
  const callPct = call?.open_interest && maxOi > 0 ? (call.open_interest / maxOi) * 100 : 0;
  const putPct = put?.open_interest && maxOi > 0 ? (put.open_interest / maxOi) * 100 : 0;
  const pcr = call?.open_interest && put?.open_interest && call.open_interest > 0 ? put.open_interest / call.open_interest : null;

  const callMoneyness = classifyStrike(strike, atmStrike, "call");
  const putMoneyness = classifyStrike(strike, atmStrike, "put");
  const callBg = callMoneyness ? MONEYNESS_BG.call[callMoneyness] : "";
  const putBg = putMoneyness ? MONEYNESS_BG.put[putMoneyness] : "";
  const isAtm = strike === atmStrike;

  return (
    <tr>
      <Td className={cn("text-right", callBg)}>
        <div className="flex items-center justify-end gap-1.5">
          <div className="h-1.5 w-10 overflow-hidden rounded-full bg-surface-elevated">
            <div className="h-full bg-positive/60" style={{ width: `${callPct}%` }} />
          </div>
          {fmt(call?.open_interest, 0)}
        </div>
      </Td>
      <Td className={cn("text-right", callBg)}><ChangeCell value={call?.open_interest_change} /></Td>
      <Td className={cn("text-right", callBg)}><ChangeCell value={call?.ltp_change} /></Td>
      <Td className={cn("text-right font-financial font-medium", callBg)}>{fmt(call?.ltp)}</Td>
      <Td className={cn("text-center font-financial font-semibold", isAtm ? "bg-active-soft text-active" : "bg-surface-elevated text-text-primary")}>
        {fmt(strike, 0)}
        {isAtm && <span className="ml-1 text-[10px] font-medium uppercase tracking-wide">ATM</span>}
      </Td>
      <Td className={cn("text-right font-financial font-medium", putBg)}>{fmt(put?.ltp)}</Td>
      <Td className={cn("text-right", putBg)}><ChangeCell value={put?.ltp_change} /></Td>
      <Td className={cn("text-right", putBg)}><ChangeCell value={put?.open_interest_change} /></Td>
      <Td className={cn("text-right", putBg)}>
        <div className="flex items-center gap-1.5">
          {fmt(put?.open_interest, 0)}
          <div className="h-1.5 w-10 overflow-hidden rounded-full bg-surface-elevated">
            <div className="h-full bg-negative/60" style={{ width: `${putPct}%` }} />
          </div>
        </div>
      </Td>
      <Td className="text-right text-text-secondary">{pcr != null ? pcr.toFixed(2) : "--"}</Td>
    </tr>
  );
}

export default function OptionsPage() {
  const { data: underlyings, isLoading: underlyingsLoading, isError: underlyingsError } = useOptionUnderlyings();
  // No sync-into-state effect: an explicit selection always wins once made,
  // otherwise this just derives to the first underlying/expiry available --
  // same pattern as ChartsPage's deep-link resolution.
  const [explicitUnderlyingId, setExplicitUnderlyingId] = useState<string | null>(null);
  const underlyingId = explicitUnderlyingId ?? underlyings?.[0]?.instrument_id ?? "";

  const { data: expiries } = useOptionExpiries(underlyingId || null);
  const optionExpiries = useMemo(() => (expiries ?? []).filter((e) => e.option_count > 0), [expiries]);
  const [explicitExpiry, setExplicitExpiry] = useState<string | null>(null);
  const expiry = optionExpiries.some((e) => e.expiry === explicitExpiry) ? (explicitExpiry as string) : (optionExpiries[0]?.expiry ?? "");

  const [timeframe, setTimeframe] = useState("15m");

  const { data: chain, isLoading: chainLoading, isError: chainError } = useOptionChain(underlyingId || null, expiry || null);
  const { data: pcrSeries } = useOptionPcr(underlyingId || null, expiry || null, timeframe);

  const instrumentIds = useMemo(
    () => (chain ?? []).flatMap((r) => [r.call?.instrument_id, r.put?.instrument_id]).filter((id): id is string => !!id),
    [chain],
  );
  const { status, prices } = useMarketDataSocket(instrumentIds);

  const mergedRows = useMemo(
    () =>
      (chain ?? []).map((row) => ({
        strike: row.strike,
        call: mergeLeg(row.call, row.call ? prices[row.call.instrument_id] : undefined),
        put: mergeLeg(row.put, row.put ? prices[row.put.instrument_id] : undefined),
      })),
    [chain, prices],
  );

  // ATM strike has no direct spot feed for the underlying index itself
  // (kite_ticker_service only subscribes NFO contracts, not the NSE index
  // row) -- approximated instead via put-call parity: at the true ATM
  // strike, a call and put of the same strike/expiry trade at very close
  // to the same premium, so the strike minimizing |call LTP - put LTP| is
  // the best estimate available from data this page already has.
  const atmStrike = useMemo(() => {
    let best: { strike: number; diff: number } | null = null;
    for (const row of mergedRows) {
      if (row.call?.ltp == null || row.put?.ltp == null) continue;
      const diff = Math.abs(row.call.ltp - row.put.ltp);
      if (!best || diff < best.diff) best = { strike: row.strike, diff };
    }
    return best?.strike ?? null;
  }, [mergedRows]);

  const maxOi = useMemo(
    () => Math.max(1, ...mergedRows.flatMap((r) => [r.call?.open_interest ?? 0, r.put?.open_interest ?? 0])),
    [mergedRows],
  );

  const pcrLines: OverlayLine[] = useMemo(
    () => [
      {
        id: "PCR",
        color: "#3b6bf5",
        points: (pcrSeries ?? []).map((p) => ({ ts: p.ts, value: p.pcr })),
      },
    ],
    [pcrSeries],
  );
  const oiChangeLines: OverlayLine[] = useMemo(
    () => [
      { id: "Call OI Chg", color: "#15803d", points: (pcrSeries ?? []).map((p) => ({ ts: p.ts, value: p.call_oi_change })) },
      { id: "Put OI Chg", color: "#b91c1c", points: (pcrSeries ?? []).map((p) => ({ ts: p.ts, value: p.put_oi_change })) },
    ],
    [pcrSeries],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Options Dashboard</h1>
          <p className="text-sm text-text-muted">NFO option chain, PCR, and open interest -- live via Kite WebSocket where connected.</p>
        </div>
        <ConnectionStatusBadge status={status} />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Select value={underlyingId} onChange={(e) => { setExplicitUnderlyingId(e.target.value); setExplicitExpiry(null); }} className="w-48">
          {!underlyings?.length && <option value="">No underlyings</option>}
          {underlyings?.map((u) => (
            <option key={u.instrument_id} value={u.instrument_id}>{u.symbol}</option>
          ))}
        </Select>
        <Select value={expiry} onChange={(e) => setExplicitExpiry(e.target.value)} className="w-40" disabled={!optionExpiries.length}>
          {!optionExpiries.length && <option value="">No expiries</option>}
          {optionExpiries.map((e) => (
            <option key={e.expiry} value={e.expiry}>{e.expiry} ({e.option_count})</option>
          ))}
        </Select>
      </div>

      <HistoryDepthCard underlyingId={underlyingId} expiry={expiry} />

      <Card>
        <CardHeader className="flex-wrap gap-3">
          <CardTitle>Put-Call Ratio</CardTitle>
          <div className="flex items-center gap-2">
            <Badge tone="neutral">Sum(Put OI) / Sum(Call OI)</Badge>
            <Select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="w-24">
              {PCR_TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {!pcrSeries?.length ? (
            <EmptyState title="No PCR data" description="No candles stored at this timeframe for this expiry yet." />
          ) : (
            <OscillatorChart lines={pcrLines} height={240} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-wrap gap-3">
          <CardTitle>Option Chain</CardTitle>
          <div className="flex items-center gap-1.5">
            <Badge tone="positive">ITM Call</Badge>
            <Badge tone="negative">ITM Put</Badge>
            <Badge tone="active">ATM</Badge>
          </div>
        </CardHeader>
        <CardContent className="overflow-x-auto p-0">
          {underlyingsLoading || chainLoading ? (
            <LoadingState />
          ) : underlyingsError || chainError ? (
            <ErrorState description="Could not load the option chain." />
          ) : !underlyingId || !expiry ? (
            <EmptyState title="No NFO underlying yet" description="Backfill NIFTY/BANKNIFTY option contracts first." />
          ) : !chain?.length ? (
            <EmptyState title="No strikes for this expiry" description="This expiry has no backfilled option contracts." />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th className="text-right">Call OI</Th>
                  <Th className="text-right">Chg</Th>
                  <Th className="text-right">LTP Chg</Th>
                  <Th className="text-right">LTP</Th>
                  <Th className="text-center">Strike</Th>
                  <Th className="text-right">LTP</Th>
                  <Th className="text-right">LTP Chg</Th>
                  <Th className="text-right">Chg</Th>
                  <Th className="text-right">Put OI</Th>
                  <Th className="text-right">PCR</Th>
                </tr>
              </Thead>
              <Tbody>
                {mergedRows.map((row) => (
                  <ChainRow key={row.strike} strike={row.strike} call={row.call} put={row.put} maxOi={maxOi} atmStrike={atmStrike} />
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Change in Open Interest</CardTitle>
        </CardHeader>
        <CardContent>
          {!pcrSeries?.length ? (
            <EmptyState title="No OI data" description="No candles stored at this timeframe for this expiry yet." />
          ) : (
            <OscillatorChart lines={oiChangeLines} height={240} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
