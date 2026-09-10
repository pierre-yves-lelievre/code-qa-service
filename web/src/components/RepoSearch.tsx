import { type FormEvent, useEffect, useState } from "react";
import { api, errorText, type IndexAccepted, type RepoHit, type RepoSearchResponse } from "../api";

const DEBOUNCE_MS = 300;

const BUTTON =
  "rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50";

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
    <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-4">
      <div>
        <label htmlFor="search" className="text-sm font-medium">
          Search GitHub
        </label>
        <input
          id="search"
          type="search"
          value={query}
          maxLength={256}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="full-stack-fastapi-template"
          className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
          autoFocus
        />
        {searching && <p className="mt-2 text-sm text-slate-500">Searching…</p>}
        {searchError && <p className="mt-2 text-sm text-red-700">{searchError}</p>}
      </div>

      {hits.length > 0 && (
        <ul className="divide-y divide-slate-100">
          {hits.map((hit) => (
            <li key={hit.full_name} className="flex items-start justify-between gap-4 py-3">
              <div className="min-w-0">
                <a
                  href={hit.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-blue-700 hover:underline"
                >
                  {hit.full_name}
                </a>
                {hit.description && (
                  <p className="truncate text-sm text-slate-600">{hit.description}</p>
                )}
                <p className="text-xs text-slate-500">
                  ★ {hit.stars.toLocaleString()} · {hit.language ?? "no language"} ·{" "}
                  {formatSize(hit.size_kb)} · {hit.default_branch}
                </p>
              </div>
              <button className={BUTTON} disabled={posting} onClick={() => startIndex(hit.url)}>
                Index
              </button>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={submitUrl} className="border-t border-slate-100 pt-4">
        <label htmlFor="url" className="text-sm font-medium">
          or paste a GitHub URL
        </label>
        <div className="mt-1 flex gap-2">
          <input
            id="url"
            value={url}
            maxLength={512}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://github.com/owner/repo or …/tree/branch"
            className="w-full rounded border border-slate-300 px-3 py-2"
          />
          <button type="submit" className={BUTTON} disabled={posting || !url.trim()}>
            Index
          </button>
        </div>
      </form>
      {indexError && <p className="text-sm text-red-700">{indexError}</p>}
    </section>
  );
}
