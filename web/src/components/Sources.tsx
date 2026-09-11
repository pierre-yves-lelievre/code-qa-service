import type { Source } from "../api";

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

/** The cited chunks, in first-cited order: `path:lines` linked to GitHub at the snapshot's commit. */
export default function Sources({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null;
  return (
    <div>
      <h3 className="caption">Sources</h3>
      <ol className="mt-2 space-y-3">
        {sources.map((source, index) => {
          const lines = `${source.path}:${source.start_line}–${source.end_line}`;
          // The link is built by the server; anything else is shown as text, never as a link.
          const linkable = source.github_url.startsWith("https://github.com/");
          return (
            <li key={lines} className="overflow-hidden rounded-lg bg-white ring-1 ring-slate-200">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-slate-100 bg-slate-50 px-3 py-2 text-xs">
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
              <pre className="max-h-56 overflow-auto px-4 py-3 font-mono text-xs leading-relaxed text-slate-800">
                {source.excerpt}
              </pre>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
