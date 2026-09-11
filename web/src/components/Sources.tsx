import type { Source, Tier } from "../api";

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

/** The cited chunks, in first-cited order: `path:lines` linked to GitHub, and the excerpt as code. */
export default function Sources({ sources, anchor }: { sources: Source[]; anchor: string }) {
  if (sources.length === 0) return null;
  return (
    <div>
      <h3 className="caption">Sources ({sources.length})</h3>
      <ol className="mt-2 space-y-3">
        {sources.map((source, index) => {
          const lines = `${source.path}:${source.start_line}–${source.end_line}`;
          // The link is built by the server; anything else is shown as text, never as a link.
          const linkable = source.github_url.startsWith("https://github.com/");
          // The excerpt drops each chunk's header line, so its first line is `start_line`.
          const code = source.excerpt.split("\n");
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
                {source.tier && (
                  <span className={`badge ml-auto tier-${source.tier}`}>{source.tier}</span>
                )}
              </div>
              <div
                className={`max-h-72 overflow-auto border-l-[3px] bg-slate-900 ${source.tier ? TIER_BORDER[source.tier] : "border-l-slate-600"}`}
              >
                <div className="min-w-max py-3 font-mono text-[12.5px] leading-6 text-slate-100">
                  {code.map((line, i) => (
                    <div key={i} className="flex">
                      <span className="sticky left-0 w-14 shrink-0 bg-slate-900 pr-4 text-right text-slate-500 tabular-nums select-none">
                        {source.start_line + i}
                      </span>
                      <span className="pr-6 whitespace-pre">{line || " "}</span>
                    </div>
                  ))}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
