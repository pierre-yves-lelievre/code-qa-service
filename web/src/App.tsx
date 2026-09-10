import { useEffect, useState } from "react";
import { api, errorText, type IndexAccepted, type RepoResponse } from "./api";
import Chat from "./components/Chat";
import IndexProgress from "./components/IndexProgress";
import RepoSearch from "./components/RepoSearch";

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

  /** Back to the search: no repository, no job, nothing in the URL. */
  function reset() {
    setRepo(null);
    setRepoId(null);
    setJob(null);
    setAlreadyIndexed(false);
    setRepoInUrl(null);
  }

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
  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Code Q&amp;A</h1>
          {repo ? (
            <a
              href={repo.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-blue-700 hover:underline"
            >
              {repo.owner}/{repo.name}
            </a>
          ) : (
            <p className="text-sm text-slate-500">Ask questions about a public GitHub repository.</p>
          )}
        </div>
        {repo && (
          <button
            onClick={() => {
              setError(null);
              reset();
            }}
            className="text-sm text-blue-700 hover:underline"
          >
            Change repository
          </button>
        )}
      </header>

      <main className="space-y-6">
        {error && <p className="text-sm text-red-700">{error}</p>}
        {loading ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : repo || job ? (
          <>
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
            {repo && <Chat key={repo.repo_id} repo={repo} />}
          </>
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
