import type { Source } from "../api";

const TIER_STYLE: Record<string, string> = {
  symbol: "bg-emerald-100 text-emerald-800",
  fts: "bg-amber-100 text-amber-800",
  vector: "bg-sky-100 text-sky-800",
};

/** The cited chunks: `path:lines` linked to GitHub at the snapshot's commit, and the excerpt. */
export default function Sources({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null;
  return (
    <div className="mt-3">
      <h3 className="text-xs font-medium tracking-wide text-slate-500 uppercase">Sources</h3>
      <ol className="mt-1 space-y-2">
        {sources.map((source, index) => {
          const lines = `${source.path}:${source.start_line}–${source.end_line}`;
          // The link is built by the server; anything else is shown as text, never as a link.
          const linkable = source.github_url.startsWith("https://github.com/");
          return (
            <li key={lines} className="rounded border border-slate-200 bg-white">
              <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-3 py-1.5 text-sm">
                <span className="text-slate-400">[{index + 1}]</span>
                {linkable ? (
                  <a
                    href={source.github_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-mono text-blue-700 hover:underline"
                  >
                    {lines}
                  </a>
                ) : (
                  <span className="font-mono">{lines}</span>
                )}
                {source.qualname && <code className="text-slate-600">{source.qualname}</code>}
                {source.tier && (
                  <span className={`rounded px-1.5 text-xs ${TIER_STYLE[source.tier]}`}>
                    {source.tier}
                  </span>
                )}
              </div>
              <pre className="max-h-48 overflow-auto px-3 py-2 text-xs">{source.excerpt}</pre>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
