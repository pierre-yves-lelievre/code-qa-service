import { useLayoutEffect, useRef, useState } from "react";
import type { Source, Tier } from "../api";

// What each retrieval tier means; the badge's tooltip here and in the trace.
export const TIER_HINT: Record<Tier, string> = {
  symbol: "Exact name match",
  fts: "Keyword match",
  vector: "Semantic similarity",
};

// The code panel's left border, in the tier's colour (the badge above uses the same hue).
const TIER_BORDER: Record<Tier, string> = {
  symbol: "border-l-tier-symbol",
  fts: "border-l-tier-fts",
  vector: "border-l-tier-vector",
};

const EXTERNAL = (
  <svg
    viewBox="0 0 24 24"
    className="size-3 shrink-0"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
  </svg>
);

/** The excerpt as an editor panel: line numbers from `start_line`, a fade when lines overflow. */
function CodePanel({ source }: { source: Source }) {
  const scroller = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element) setOverflows(element.scrollWidth > element.clientWidth + 1);
  }, [source.excerpt]);
  // The excerpt drops each chunk's header line, so its first line is `start_line`.
  const code = source.excerpt.split("\n");
  return (
    <div
      className={`relative border-l-[3px] bg-slate-900 ${source.tier ? TIER_BORDER[source.tier] : "border-l-slate-600"}`}
    >
      <div ref={scroller} className="max-h-72 overflow-auto">
        <div className="min-w-max py-3 font-mono text-[12.5px] leading-6 text-slate-100">
          {code.map((line, i) => (
            <div key={i} className="flex">
              <span className="sticky left-0 w-11 shrink-0 bg-slate-900 pr-3 text-right text-slate-500 tabular-nums select-none">
                {source.start_line + i}
              </span>
              <span className="pr-8 whitespace-pre">{line || " "}</span>
            </div>
          ))}
        </div>
      </div>
      {overflows && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 w-16 bg-linear-to-l from-slate-900 to-transparent"
        />
      )}
    </div>
  );
}

/** The cited chunks, in first-cited order: `path:lines` linked to GitHub, and the excerpt as code. */
export default function Sources({
  sources,
  anchor,
  onAskAbout,
}: {
  sources: Source[];
  anchor: string;
  onAskAbout: (question: string) => void;
}) {
  if (sources.length === 0) return null;
  return (
    <div>
      <h3 className="caption">Sources ({sources.length})</h3>
      <ol className="mt-2 space-y-3">
        {sources.map((source, index) => {
          const lines = `${source.path}:${source.start_line}–${source.end_line}`;
          // The link is built by the server; anything else is shown as text, never as a link.
          const linkable = source.github_url.startsWith("https://github.com/");
          return (
            <li
              key={lines}
              id={`${anchor}-${index + 1}`}
              className="scroll-mt-24 overflow-hidden rounded-lg bg-white shadow-sm ring-1 ring-slate-200"
            >
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs">
                <span className="flex size-5 shrink-0 items-center justify-center rounded bg-white font-medium text-slate-500 tabular-nums ring-1 ring-slate-200">
                  {index + 1}
                </span>
                {linkable ? (
                  <a
                    href={source.github_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={lines}
                    className="link inline-flex min-w-0 items-center gap-1 font-mono"
                  >
                    <span className="truncate">{lines}</span>
                    {EXTERNAL}
                  </a>
                ) : (
                  <span className="truncate font-mono text-slate-700" title={lines}>
                    {lines}
                  </span>
                )}
                {source.qualname && (
                  <code className="truncate font-mono text-slate-500" title={source.qualname}>
                    {source.qualname}
                  </code>
                )}
                <button
                  type="button"
                  onClick={() => onAskAbout(`Explain ${source.qualname ?? source.path}`)}
                  className="ml-auto rounded-md px-1.5 py-0.5 font-medium text-accent transition-colors hover:bg-accent-soft"
                >
                  Ask about this
                </button>
                {source.tier && (
                  <span className={`badge tier-${source.tier}`} title={TIER_HINT[source.tier]}>
                    {source.tier}
                  </span>
                )}
              </div>
              <CodePanel source={source} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}
