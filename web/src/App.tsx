import { useEffect, useState } from "react";
import { api, errorText, type HealthResponse, type IndexAccepted, type RepoResponse } from "./api";
import Chat from "./components/Chat";
import IndexProgress from "./components/IndexProgress";
import RepoSearch from "./components/RepoSearch";

// The product mark: an accent tile with a code glyph; the favicon in index.html is the same.
const MARK = (
  <svg viewBox="0 0 32 32" className="size-8 shrink-0" aria-hidden="true">
    <rect width="32" height="32" rx="8" className="fill-accent" />
    <path
      d="M13 11l-5 5 5 5M19 11l5 5-5 5"
      fill="none"
      stroke="white"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

/** The repository id in `?repo=`, or null. */
function repoFromUrl(): number | null {
  const value = new URLSearchParams(window.location.search).get("repo");
  return value !== null && /^\d+$/.test(value) ? Number(value) : null;
}

/** Put the repository id in the URL, or remove it, without reloading. */
function setRepoInUrl(repoId: number | null): void {
  window.history.replaceState(null, "", repoId === null ? window.location.pathname : `?repo=${repoId}`);
}

/** The page: search and index a repository, then its summary and the chat; `?repo=` survives a reload. */
export default function App() {
  const [repoId, setRepoId] = useState(repoFromUrl);
  const [repo, setRepo] = useState<RepoResponse | null>(null);
  const [job, setJob] = useState<IndexAccepted | null>(null);
  const [alreadyIndexed, setAlreadyIndexed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<HealthResponse["providers"] | null>(null);

  /** Back to the search: no repository, no job, nothing in the URL. */
  function reset() {
    setRepo(null);
    setRepoId(null);
    setJob(null);
    setAlreadyIndexed(false);
    setRepoInUrl(null);
  }

  useEffect(() => {
    // Cosmetic: the pill says which providers answer, so a failed check shows nothing.
    api<HealthResponse>("GET", "/health").then(
      (health) => setProviders(health.providers),
      () => setProviders(null),
    );
  }, []);

  useEffect(() => {
    if (repoId === null) return;
    let live = true;
    api<RepoResponse>("GET", `/repos/${repoId}`).then(
      (found) => {
        if (!live) return;
        if (found.snapshot) {
          setRepo(found);
          setJob(null);
        } else {
          setError("This repository has no indexed snapshot yet; index it again.");
          reset();
        }
      },
      (e) => {
        if (!live) return;
        setError(errorText(e));
        reset();
      },
    );
    return () => {
      live = false;
    };
  }, [repoId]);

  const loading = repoId !== null && repo === null && job === null;
  const progress = (
    <IndexProgress
      job={job}
      repo={repo}
      alreadyIndexed={alreadyIndexed}
      onSucceeded={(already) => {
        if (!job) return;
        setAlreadyIndexed(already);
        setRepoInUrl(job.repo_id);
        setRepoId(job.repo_id);
      }}
      onBack={() => setJob(null)}
    />
  );
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            {MARK}
            <div className="min-w-0">
              <p className="text-sm font-semibold text-slate-900">Code Q&amp;A</p>
              <p className="hidden truncate text-xs text-slate-500 sm:block">
                Ask questions about a public GitHub repository; answers cite the code.
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {providers === "fake" && (
              <span
                className="badge gap-1.5 bg-white text-slate-600 ring-slate-300"
                title="PROVIDERS=fake: fake vectors and canned answers"
              >
                <span className="size-1.5 rounded-full bg-slate-400" />
                fake providers
              </span>
            )}
            {repo && (
              <button
                onClick={() => {
                  setError(null);
                  reset();
                }}
                className="btn-secondary py-1.5"
              >
                <span className="sm:hidden">Change</span>
                <span className="hidden sm:inline">Change repository</span>
              </button>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:py-8">
        {error && <p className="error-note mx-auto mb-6 max-w-2xl">{error}</p>}
        {loading ? (
          <p className="animate-pulse text-center text-sm text-slate-500">Loading the repository…</p>
        ) : repo ? (
          <Chat key={repo.repo_id} repo={repo} aside={progress} />
        ) : job ? (
          <div className="mx-auto max-w-xl py-6">{progress}</div>
        ) : (
          <RepoSearch
            onStarted={(accepted) => {
              setError(null);
              setJob(accepted);
            }}
          />
        )}
      </main>
    </div>
  );
}
