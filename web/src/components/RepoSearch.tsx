import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { api, errorText, type IndexAccepted, type RepoHit, type RepoSearchResponse } from "../api";

const DEBOUNCE_MS = 300;

// Chips that fill the search box; they never start an index.
const EXAMPLES = ["full-stack-fastapi-template", "markupsafe"];

/** The search glyph at a given size. */
function searchIcon(className: string) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
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
}

const CODE = (
  <svg
    viewBox="0 0 24 24"
    className="size-4"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m8 7-5 5 5 5M16 7l5 5-5 5" />
  </svg>
);

// Same sparkle as Chat's suggested questions.
const SPARKLE = (
  <svg viewBox="0 0 24 24" className="size-4" fill="currentColor" aria-hidden="true">
    <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
  </svg>
);

const STAR = (
  <svg viewBox="0 0 24 24" className="size-3.5 text-slate-400" fill="currentColor" aria-hidden="true">
    <path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4l-5.9 3.1 1.2-6.5-4.8-4.6 6.6-.9z" />
  </svg>
);

// How it works, shown while nothing is being searched.
const STEPS: { icon: ReactNode; title: string; text: string }[] = [
  { icon: searchIcon("size-4"), title: "Search", text: "Find any public GitHub repository." },
  { icon: CODE, title: "Index", text: "Every symbol parsed, every chunk embedded." },
  { icon: SPARKLE, title: "Ask", text: "Answers cite the files and lines they use." },
];

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
  const [pasting, setPasting] = useState(false);
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
    <div className="relative isolate">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-x-4 -top-8 -z-10 h-[38rem] [mask-image:radial-gradient(ellipse_70%_75%_at_50%_20%,black_45%,transparent_100%)] sm:-inset-x-6"
      >
        <div className="hero-wash absolute inset-0" />
        <div className="hero-grid absolute inset-0" />
      </div>

      <div className="mx-auto max-w-2xl py-8 lg:py-16">
        <div className="text-center">
          <h1 className="font-display text-5xl leading-[1.05] tracking-tight text-balance text-slate-900 sm:text-6xl">
            Ask anything about a codebase
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-base text-pretty text-slate-600 sm:text-lg">
            Point it at a public GitHub repository. It reads the code, then answers your questions
            with the exact files and lines behind every claim.
          </p>
        </div>

        <div className="panel mt-10 flex items-center gap-2 px-4 focus-within:ring-2 focus-within:ring-accent">
          <span className="text-slate-400">{searchIcon("size-5")}</span>
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
              className="lift rounded-full bg-white px-3 py-1 font-mono text-slate-700 ring-1 ring-slate-200 hover:text-accent hover:ring-indigo-300 focus-visible:outline-2 focus-visible:outline-accent"
            >
              {example}
            </button>
          ))}
        </div>
        {searchError && <p className="error-note mt-4">{searchError}</p>}

        {pasting ? (
          <form onSubmit={submitUrl} className="mx-auto mt-5 flex max-w-xl gap-2">
            <label htmlFor="url" className="sr-only">
              GitHub URL
            </label>
            <input
              id="url"
              value={url}
              maxLength={512}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://github.com/owner/repo"
              className="input font-mono"
              autoFocus
            />
            <button type="submit" className="btn-secondary shrink-0" disabled={posting || !url.trim()}>
              Index
            </button>
          </form>
        ) : (
          <p className="mt-4 text-center text-sm text-slate-500">
            Have a URL?{" "}
            <button type="button" onClick={() => setPasting(true)} className="link font-medium">
              Paste it
            </button>
          </p>
        )}
        {indexError && <p className="error-note mx-auto mt-3 max-w-xl">{indexError}</p>}

        {!query.trim() && (
          <ol className="mt-12 grid gap-3 sm:grid-cols-3">
            {STEPS.map((step, index) => (
              <li
                key={step.title}
                className="relative rounded-xl bg-slate-900 p-4 shadow-sm ring-1 ring-slate-900/10"
              >
                <div className="flex items-center gap-2.5">
                  <span className="flex size-7 items-center justify-center rounded-lg bg-white/10 text-indigo-300">
                    {step.icon}
                  </span>
                  <span className="text-sm font-semibold text-white">{step.title}</span>
                  <span className="ml-auto font-mono text-xs text-slate-500">0{index + 1}</span>
                </div>
                <p className="mt-2.5 text-sm text-slate-300">{step.text}</p>
                {index < STEPS.length - 1 && (
                  <span
                    aria-hidden="true"
                    className="absolute top-1/2 -right-3 z-10 hidden -translate-y-1/2 text-slate-400 sm:block"
                  >
                    →
                  </span>
                )}
              </li>
            ))}
          </ol>
        )}

        {hits.length > 0 && (
          <ul className="mt-6 space-y-3">
            {hits.map((hit) => (
              <li
                key={hit.full_name}
                className="panel lift flex items-center justify-between gap-4 px-5 py-4"
              >
                <div className="min-w-0">
                  <a
                    href={hit.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="link font-mono text-sm font-medium break-all"
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
      </div>
    </div>
  );
}
