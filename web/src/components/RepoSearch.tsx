import { type FormEvent, useEffect, useState } from "react";
import { api, errorText, type IndexAccepted, type RepoHit, type RepoSearchResponse } from "../api";

const DEBOUNCE_MS = 300;

// Chips that fill the search box; they never start an index.
const EXAMPLES = ["full-stack-fastapi-template", "markupsafe"];

const SEARCH = (
  <svg
    viewBox="0 0 24 24"
    className="size-5"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

const STAR = (
  <svg viewBox="0 0 24 24" className="size-3.5 text-slate-400" fill="currentColor" aria-hidden="true">
    <path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4l-5.9 3.1 1.2-6.5-4.8-4.6 6.6-.9z" />
  </svg>
);

/** "812 KB" or "27.3 MB" from GitHub's size in kilobytes. */
function formatSize(kb: number): string {
  return kb < 1024 ? `${kb} KB` : `${(kb / 1024).toFixed(1)} MB`;
}

/** Debounced GitHub search with an Index button per result, and a paste-a-URL fallback. */
export default function RepoSearch({ onStarted }: { onStarted: (job: IndexAccepted) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<RepoHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [posting, setPosting] = useState(false);
  const [indexError, setIndexError] = useState<string | null>(null);

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits([]);
      setSearching(false);
      setSearchError(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setSearching(true);
      try {
        const path = `/repos/search?q=${encodeURIComponent(q)}`;
        const result = await api<RepoSearchResponse>("GET", path, undefined, controller.signal);
        setHits(result.items);
        setSearchError(null);
      } catch (error) {
        if (controller.signal.aborted) return;
        setHits([]);
        setSearchError(errorText(error));
      }
      setSearching(false);
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  /** Start an index job for a repository URL; the server validates it. */
  async function startIndex(target: string) {
    setPosting(true);
    setIndexError(null);
    try {
      onStarted(await api<IndexAccepted>("POST", "/index", { url: target }));
    } catch (error) {
      setIndexError(errorText(error));
      setPosting(false);
    }
  }

  /** Index the pasted URL. */
  function submitUrl(event: FormEvent) {
    event.preventDefault();
    if (url.trim()) void startIndex(url.trim());
  }

  return (
    <div className="mx-auto max-w-2xl py-6 lg:py-14">
      <div className="text-center">
        <h1 className="text-3xl font-semibold tracking-tight text-balance text-slate-900 sm:text-4xl">
          Ask anything about a codebase
        </h1>
        <p className="mx-auto mt-3 max-w-xl text-base text-pretty text-slate-600">
          Search a public GitHub repository and index it, then ask questions. Every answer cites the
          files and lines it comes from.
        </p>
      </div>

      <div className="panel mt-10 flex items-center gap-2 px-4 focus-within:ring-2 focus-within:ring-accent">
        <span className="text-slate-400">{SEARCH}</span>
        <label htmlFor="search" className="sr-only">
          Search GitHub
        </label>
        <input
          id="search"
          type="search"
          value={query}
          maxLength={256}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search GitHub repositories"
          className="w-full bg-transparent py-3.5 text-base text-slate-900 outline-none placeholder:text-slate-400"
          autoFocus
        />
        {searching && <span className="text-xs whitespace-nowrap text-slate-400">Searching…</span>}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-center gap-2 text-xs text-slate-500">
        <span>Try</span>
        {EXAMPLES.map((example) => (
          <button
            key={example}
            onClick={() => setQuery(example)}
            className="rounded-full bg-white px-3 py-1 font-mono text-slate-700 ring-1 ring-slate-200 transition-colors hover:text-accent hover:ring-indigo-300 focus-visible:outline-2 focus-visible:outline-accent"
          >
            {example}
          </button>
        ))}
      </div>
      {searchError && <p className="error-note mt-4">{searchError}</p>}

      {hits.length > 0 && (
        <ul className="panel mt-6 divide-y divide-slate-100 overflow-hidden">
          {hits.map((hit) => (
            <li
              key={hit.full_name}
              className="flex items-center justify-between gap-4 px-5 py-4 transition-colors hover:bg-slate-50"
            >
              <div className="min-w-0">
                <a
                  href={hit.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="link text-sm font-semibold break-all"
                >
                  {hit.full_name}
                </a>
                {hit.description && (
                  <p className="mt-1 line-clamp-2 text-sm text-slate-600">{hit.description}</p>
                )}
                <p className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
                  <span className="inline-flex items-center gap-1 tabular-nums">
                    {STAR}
                    {hit.stars.toLocaleString()}
                  </span>
                  <span>{hit.language ?? "No language"}</span>
                  <span className="tabular-nums">{formatSize(hit.size_kb)}</span>
                  <span className="font-mono">{hit.default_branch}</span>
                </p>
              </div>
              <button
                className="btn-primary shrink-0"
                disabled={posting}
                onClick={() => startIndex(hit.url)}
              >
                Index
              </button>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={submitUrl} className="mt-10">
        <label htmlFor="url" className="text-sm font-medium text-slate-700">
          Or paste a GitHub URL
        </label>
        <div className="mt-2 flex gap-2">
          <input
            id="url"
            value={url}
            maxLength={512}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://github.com/owner/repo"
            className="input font-mono"
          />
          <button type="submit" className="btn-secondary shrink-0" disabled={posting || !url.trim()}>
            Index
          </button>
        </div>
      </form>
      {indexError && <p className="error-note mt-3">{indexError}</p>}
    </div>
  );
}
