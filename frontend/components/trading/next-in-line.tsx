"use client";

/**
 * 52 Week High Breakout's "next in line" (20 Nov): under the card's holdings,
 * the stocks its Friday 15:00 check would take next -- breakouts (above the
 * highest weekly high of the 52 weeks before this one) strongest first, the
 * order it buys in while a slot is free, then the nearest below that level.
 * Read-only; re-read every minute.
 */

import { Badge } from "@/components/ui/badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { useNextInLine } from "@/lib/hooks";
import { istShortTime } from "@/lib/time";

const price = (value: number) => value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function NextInLine({ deploymentId }: { deploymentId: string }) {
  const { data, isLoading, isError } = useNextInLine(deploymentId, true);
  const cell = "px-3 py-2 whitespace-nowrap";

  return (
    <div className="space-y-2" aria-label="Next in line">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h4 className="text-sm font-semibold text-text-primary">Next in line</h4>
        {data && (
          <span className="text-xs text-text-secondary">
            {data.slots_free} of {data.slots_total} slots free · next check {data.next_check}
          </span>
        )}
      </div>
      {isLoading && <p className="text-xs text-text-muted">Scanning the 500 stocks…</p>}
      {isError && <p className="text-xs text-negative">The scanner couldn&apos;t be read just now; it tries again in a minute.</p>}
      {data && data.rows.length === 0 && <p className="text-xs text-text-muted">No stock with a full 52 weeks of candles to rank yet.</p>}
      {data && data.rows.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border">
          <Table className="text-sm">
            <Thead>
              <tr>
                <Th className="px-3">#</Th>
                <Th className="px-3">Stock</Th>
                <Th className="px-3 text-right">{data.price_source === "live" ? "Price" : "Last close"}</Th>
                <Th className="px-3 text-right">52-wk high</Th>
                <Th className="px-3 text-right">Gap</Th>
              </tr>
            </Thead>
            <Tbody>
              {data.rows.map((row, i) => (
                <tr key={row.symbol}>
                  <Td className={`${cell} text-text-muted`}>{i + 1}</Td>
                  <Td className={`${cell} font-medium`}>{row.symbol}</Td>
                  <Td className={`${cell} text-right font-financial`}>{price(row.price)}</Td>
                  <Td className={`${cell} text-right font-financial text-text-secondary`}>{price(row.level)}</Td>
                  <Td className={`${cell} text-right`}>
                    {row.breakout ? (
                      <Badge tone="positive">+{row.gap_pct.toFixed(2)}% breakout</Badge>
                    ) : (
                      <span className="font-financial text-text-secondary">{Math.abs(row.gap_pct).toFixed(2)}% to go</span>
                    )}
                  </Td>
                </tr>
              ))}
            </Tbody>
          </Table>
        </div>
      )}
      {data && (
        <p className="text-xs text-text-muted">
          {data.breakouts_now > 0
            ? `${data.breakouts_now} above their 52-week high now: bought at the check, strongest first, while a slot is free; then the nearest below. `
            : "None above its 52-week high now: the nearest below. "}
          Held stocks left out. {data.price_source === "live" ? `Prices as of ${istShortTime(data.as_of)} IST.` : (data.note ?? "")}
        </p>
      )}
    </div>
  );
}
