import { useEffect, useState } from "react";
import { api, errorText, type HealthResponse, type IndexAccepted, type RepoResponse } from "./api";
import Chat from "./components/Chat";
import IndexProgress from "./components/IndexProgress";
import RepoSearch from "./components/RepoSearch";

// The product mark: a code glyph on an indigo-to-violet tile; the favicon is the same glyph.
const MARK = (
  <span
    className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-linear-to-br from-indigo-500 to-violet-600 shadow-sm shadow-indigo-500/30"
    aria-hidden="true"
  >
    <svg viewBox="6 6 20 20" className="size-5">
      <path
        d="M13 11l-5 5 5 5M19 11l5 5-5 5"
        fill="none"
        stroke="white"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  </span>
);

// A status pill on the dark header.
const PILL =
  "items-center gap-1.5 rounded-md bg-white/5 px-2 py-0.5 text-xs text-slate-400 ring-1 ring-white/10";

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
  const [health, setHealth] = useState<HealthResponse | null>(null);

  /** Back to the search: no repository, no job, nothing in the URL. */
  function reset() {
    setRepo(null);
    setRepoId(null);
    setJob(null);
    setAlreadyIndexed(false);
    setRepoInUrl(null);
  }

  useEffect(() => {
    // Read the body whatever the status: a degraded /health answers 503 with the same shape.
    // Cosmetic, so a failed check shows no pills rather than an error.
    fetch("/health")
      .then((response) => response.json() as Promise<HealthResponse>)
      .then(setHealth, () => setHealth(null));
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
  const sha = repo?.snapshot?.commit_sha?.slice(0, 7);
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
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-900">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            {MARK}
            <div className="min-w-0">
              <p className="text-base leading-tight font-semibold tracking-tight text-white">
                Code Q&amp;A
              </p>
              <p className="hidden truncate text-xs text-slate-400 sm:block">Answers that cite the code.</p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {health && (
              <span className={`${PILL} hidden md:inline-flex`}>
                DB
                <span
                  className={`size-1.5 rounded-full ${health.database.reachable ? "bg-emerald-400" : "bg-red-400"}`}
                />
                <span className="text-slate-200">{health.database.reachable ? "ok" : "down"}</span>
              </span>
            )}
            {health && (
              <span
                className={`${PILL} inline-flex`}
                title={
                  health.providers === "fake"
                    ? "PROVIDERS=fake: fake vectors and canned answers"
                    : "PROVIDERS=real: Voyage embeddings and Claude answers"
                }
              >
                providers · <span className="text-slate-200">{health.providers}</span>
              </span>
            )}
            {sha && (
              <span className={`${PILL} hidden sm:inline-flex`} title={repo?.snapshot?.commit_sha ?? ""}>
                snapshot · <span className="font-mono text-slate-200">{sha}</span>
              </span>
            )}
            {repo && (
              <button
                onClick={() => {
                  setError(null);
                  reset();
                }}
                className="ml-1 rounded-lg bg-white/10 px-3 py-1.5 text-sm font-medium text-white ring-1 ring-white/15 transition-colors hover:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400"
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
