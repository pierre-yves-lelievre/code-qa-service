import type { AskResponse, LegTrace } from "../api";

const STATUS_STYLE: Record<string, string> = {
  ok: "text-emerald-700",
  fallback: "text-amber-700",
  or_fallback: "text-amber-700",
  empty: "text-slate-500",
  skipped: "text-slate-500",
  unavailable: "text-red-700",
};

const CELL = "py-0.5 pr-3";

/** A small key/value table with a heading. */
function Rows({ title, rows }: { title: string; rows: [string, string | number][] }) {
  return (
    <table className="text-sm">
      <caption className="text-left text-xs font-medium tracking-wide text-slate-500 uppercase">
        {title}
      </caption>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <td className={`${CELL} text-slate-500`}>{label}</td>
            <td className={`${CELL} text-right tabular-nums`}>
              {typeof value === "number" ? value.toLocaleString() : value}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** How the answer was made: retrievers and planner, timings, tokens and the snapshot. */
export default function Trace({ response }: { response: AskResponse }) {
  const { retrievers, timings, tokens, snapshot } = response;
  const legs: [string, LegTrace][] = [
    ["symbol", retrievers.symbol],
    ["fts", retrievers.fts],
    ["vector", retrievers.vector],
  ];
  return (
    <details className="mt-3 rounded border border-slate-200 bg-white text-sm">
      <summary className="cursor-pointer px-3 py-1.5 text-slate-600">
        Trace · {timings.total_ms.toLocaleString()} ms · planner {retrievers.planner}
      </summary>
      <div className="grid gap-4 px-3 py-2 sm:grid-cols-2">
        <table className="text-sm">
          <caption className="text-left text-xs font-medium tracking-wide text-slate-500 uppercase">
            Retrievers
          </caption>
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className={`${CELL} font-normal`}>Leg</th>
              <th className={`${CELL} font-normal`}>Status</th>
              <th className={`${CELL} text-right font-normal`}>Hits</th>
              <th className={`${CELL} text-right font-normal`}>ms</th>
            </tr>
          </thead>
          <tbody>
            {legs.map(([name, leg]) => (
              <tr key={name}>
                <td className={CELL}>{name}</td>
                <td className={`${CELL} ${STATUS_STYLE[leg.status]}`}>{leg.status}</td>
                <td className={`${CELL} text-right tabular-nums`}>{leg.hits}</td>
                <td className={`${CELL} text-right tabular-nums`}>{leg.ms}</td>
              </tr>
            ))}
            <tr>
              <td className={CELL}>planner</td>
              <td className={`${CELL} ${STATUS_STYLE[retrievers.planner]}`}>{retrievers.planner}</td>
              <td className={CELL} />
              <td className={`${CELL} text-right tabular-nums`}>{timings.plan_ms}</td>
            </tr>
          </tbody>
        </table>
        <Rows
          title="Timings (ms)"
          rows={[
            ["plan", timings.plan_ms],
            ["embed (in retrieve)", timings.embed_ms],
            ["retrieve", timings.retrieve_ms],
            ["llm", timings.llm_ms],
            ["total", timings.total_ms],
          ]}
        />
        <Rows
          title="Tokens"
          rows={[
            ["input", tokens.input],
            ["output", tokens.output],
            ["cache read", tokens.cache_read],
            ["cache write", tokens.cache_write],
          ]}
        />
        <Rows
          title="Snapshot"
          rows={[
            ["commit", snapshot.commit_sha.slice(0, 12)],
            ["indexed", snapshot.indexed_at ? new Date(snapshot.indexed_at).toLocaleString() : "–"],
          ]}
        />
      </div>
    </details>
  );
}
