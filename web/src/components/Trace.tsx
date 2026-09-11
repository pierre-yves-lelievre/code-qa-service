import type { AskResponse, LegTrace, Tier } from "../api";

// Text only: the tier colours belong to the leg names, so statuses stay neutral unless broken.
const STATUS_STYLE: Record<string, string> = {
  ok: "text-slate-700",
  fallback: "text-slate-500",
  or_fallback: "text-slate-500",
  empty: "text-slate-500",
  skipped: "text-slate-500",
  unavailable: "text-red-700",
};

const CELL = "py-1 pr-3";
const NUM = "py-1 text-right tabular-nums";

const CHEVRON = (
  <svg
    viewBox="0 0 24 24"
    className="size-3.5 shrink-0 transition-transform group-open:rotate-90"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m9 6 6 6-6 6" />
  </svg>
);

/** A small key/value table with a caption. */
function Rows({ title, rows }: { title: string; rows: [string, string | number][] }) {
  return (
    <table className="w-full table-fixed">
      <caption className="caption mb-1 text-left">{title}</caption>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label} className="border-t border-slate-100">
            <td className={`${CELL} text-slate-500`}>{label}</td>
            <td className={`${NUM} text-slate-800`}>
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
  const legs: [Tier, LegTrace][] = [
    ["symbol", retrievers.symbol],
    ["fts", retrievers.fts],
    ["vector", retrievers.vector],
  ];
  return (
    <details className="group rounded-lg ring-1 ring-slate-200">
      <summary className="flex list-none items-center gap-1.5 px-3 py-2 text-xs text-slate-500 transition-colors select-none hover:text-slate-800 [&::-webkit-details-marker]:hidden">
        {CHEVRON}
        <span>
          Trace · {timings.total_ms.toLocaleString()} ms · planner {retrievers.planner}
        </span>
      </summary>
      <div className="grid items-start gap-x-8 gap-y-5 border-t border-slate-100 px-3 py-3 text-xs sm:grid-cols-2">
        <table className="w-full table-fixed">
          <caption className="caption mb-1 text-left">Retrievers</caption>
          <thead className="text-left text-slate-400">
            <tr>
              <th className={`${CELL} w-[34%] font-normal`}>Leg</th>
              <th className={`${CELL} font-normal`}>Status</th>
              <th className={`${NUM} w-[18%] font-normal`}>Hits</th>
              <th className={`${NUM} w-[18%] font-normal`}>ms</th>
            </tr>
          </thead>
          <tbody>
            {legs.map(([name, leg]) => (
              <tr key={name} className="border-t border-slate-100">
                <td className={CELL}>
                  <span className={`badge tier-${name}`}>{name}</span>
                </td>
                <td className={`${CELL} ${STATUS_STYLE[leg.status]}`}>{leg.status}</td>
                <td className={NUM}>{leg.hits}</td>
                <td className={NUM}>{leg.ms}</td>
              </tr>
            ))}
            <tr className="border-t border-slate-100">
              <td className={`${CELL} text-slate-500`}>planner</td>
              <td className={`${CELL} ${STATUS_STYLE[retrievers.planner]}`}>{retrievers.planner}</td>
              <td className={NUM} />
              <td className={NUM}>{timings.plan_ms}</td>
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
