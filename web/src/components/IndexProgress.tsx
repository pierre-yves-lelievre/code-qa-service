import { useEffect, useState } from "react";
import {
  api,
  errorText,
  type IndexAccepted,
  type JobProgress,
  type JobResponse,
  type RepoResponse,
  type SnapshotInfo,
} from "../api";

const POLL_MS = 1500;

// The job's stages, in order; `done` follows the last.
const STAGES: [string, string][] = [
  ["cloning", "Cloning the repository"],
  ["walking", "Listing files"],
  ["parsing", "Parsing and chunking"],
  ["embedding", "Embedding chunks"],
  ["summarizing", "Writing the summary"],
];

const CHECK = (
  <svg
    viewBox="0 0 24 24"
    className="size-3.5"
    fill="none"
    stroke="currentColor"
    strokeWidth="3"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m5 12 5 5 9-10" />
  </svg>
);

// Same spinner as Chat's.
const SPINNER = (
  <svg
    viewBox="0 0 24 24"
    className="size-4 animate-spin"
    fill="none"
    stroke="currentColor"
    strokeWidth="2.5"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="9" className="opacity-25" />
    <path d="M21 12a9 9 0 0 0-9-9" strokeLinecap="round" />
  </svg>
);

const GITHUB = (
  <svg viewBox="0 0 16 16" className="size-4 shrink-0" fill="currentColor" aria-hidden="true">
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
  </svg>
);

// GitHub's language colours, except Python, which takes its logo yellow so it never sits as a
// second blue beside TypeScript; "text" is indexing.py's windowed, no-parser bucket.
const LANGUAGE_COLORS: Record<string, string> = {
  python: "#F2B233",
  typescript: "#3178C6",
  tsx: "#61DAFB",
  javascript: "#F1E05A",
  text: "#CBD5E1",
};
const SPARE_COLORS = ["#A78BFA", "#F472B6", "#34D399", "#FB923C"];

/** A language's colour in the bar and the table; unknown ones take a spare, in order. */
function languageColor(language: string, index: number): string {
  return LANGUAGE_COLORS[language] ?? SPARE_COLORS[index % SPARE_COLORS.length];
}

/** A count with thousands separators, or a dash when the server did not send it. */
function fmt(value: number | undefined): string {
  return value === undefined ? "–" : value.toLocaleString();
}

interface Props {
  job: IndexAccepted | null;
  repo: RepoResponse | null;
  alreadyIndexed: boolean;
  onSucceeded: (alreadyIndexed: boolean) => void;
  onBack: () => void;
}

/** Polls an index job until it succeeds or fails; with an indexed repo, shows its summary. */
export default function IndexProgress({ job, repo, alreadyIndexed, onSucceeded, onBack }: Props) {
  if (repo?.snapshot) {
    return <Summary repo={repo} snapshot={repo.snapshot} alreadyIndexed={alreadyIndexed} />;
  }
  if (job) return <Progress job={job} onSucceeded={onSucceeded} onBack={onBack} />;
  return null;
}

/** The running job: a stepper with the current stage's counts and bar, polled every POLL_MS. */
function Progress({
  job,
  onSucceeded,
  onBack,
}: {
  job: IndexAccepted;
  onSucceeded: (alreadyIndexed: boolean) => void;
  onBack: () => void;
}) {
  const [state, setState] = useState<JobResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await api<JobResponse>("GET", `/index/${job.job_id}`);
        if (!live) return;
        setState(next);
        if (next.status === "succeeded") onSucceeded(Boolean(next.progress.already_indexed));
        else if (next.status !== "failed") timer = window.setTimeout(poll, POLL_MS);
      } catch (e) {
        if (live) setError(errorText(e));
      }
    };
    void poll();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // Poll once per job; a new callback identity does not restart it.
  }, [job.job_id]);

  const status = state?.status ?? "pending";
  const progress = state?.progress ?? {};
  const current =
    status === "succeeded" || progress.stage === "done"
      ? STAGES.length
      : STAGES.findIndex(([stage]) => stage === progress.stage);
  const fraction = progressFraction(progress);
  const counts = progressCounts(progress);
  return (
    <section className="panel p-6">
      <p className="caption">Indexing</p>
      <h2 className="mt-1 font-display text-3xl leading-tight break-all text-slate-900">
        {job.owner}/{job.name}{" "}
        <span className="font-mono text-sm text-slate-500">@ {job.branch}</span>
      </h2>
      {status === "failed" ? (
        <p className="error-note mt-5">{state?.error ?? "The index job failed."}</p>
      ) : (
        <>
          {status === "pending" && <p className="mt-2 text-sm text-slate-500">Waiting to start…</p>}
          {current === -1 && progress.stage && (
            <p className="mt-2 text-sm text-slate-500">{progress.stage}</p>
          )}
          <ol className="mt-6 space-y-4">
            {STAGES.map(([stage, label], index) => {
              const done = index < current;
              const active = index === current;
              return (
                <li key={stage} className="flex gap-3">
                  <span
                    className={`flex size-6 shrink-0 items-center justify-center rounded-full ${
                      done
                        ? "bg-accent-soft text-accent"
                        : active
                          ? "text-accent ring-1 ring-indigo-200"
                          : "ring-1 ring-slate-200"
                    }`}
                  >
                    {done ? CHECK : active ? SPINNER : <span className="size-1.5 rounded-full bg-slate-300" />}
                  </span>
                  <div className="min-w-0 flex-1 pt-0.5">
                    <p
                      className={`text-sm ${
                        active ? "font-medium text-slate-900" : done ? "text-slate-600" : "text-slate-400"
                      }`}
                    >
                      {label}
                    </p>
                    {active && counts && (
                      <p className="mt-0.5 text-xs text-slate-500 tabular-nums">{counts}</p>
                    )}
                    {active && fraction !== null && (
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full bg-accent transition-all duration-500"
                          style={{ width: `${Math.round(fraction * 100)}%` }}
                        />
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </>
      )}
      {error && <p className="error-note mt-5">{error}</p>}
      {(status === "failed" || error) && (
        <button onClick={onBack} className="btn-secondary mt-5">
          Back to search
        </button>
      )}
    </section>
  );
}

/** Done/total for the stages that report it, else null. */
function progressFraction(progress: JobProgress): number | null {
  if (progress.stage === "parsing" && progress.files_total) {
    return (progress.files_done ?? 0) / progress.files_total;
  }
  if (progress.stage === "embedding" && progress.chunks_total) {
    return (progress.chunks_done ?? 0) / progress.chunks_total;
  }
  return null;
}

/** The counts line for the current stage. */
function progressCounts(progress: JobProgress): string {
  switch (progress.stage) {
    case "parsing":
      return `${fmt(progress.files_done)} / ${fmt(progress.files_total)} files · ${fmt(progress.chunks)} chunks`;
    case "embedding":
      return `${fmt(progress.chunks_done)} / ${fmt(progress.chunks_total)} chunks embedded · ${fmt(progress.tokens)} tokens`;
    case "done":
      return progress.already_indexed
        ? "Already indexed at this commit."
        : `${fmt(progress.files)} files · ${fmt(progress.chunks)} chunks`;
    default:
      return "";
  }
}

/** The sidebar: repo, commit, summary, totals, per-language breakdown, skipped and unparsed files. */
function Summary({
  repo,
  snapshot,
  alreadyIndexed,
}: {
  repo: RepoResponse;
  snapshot: SnapshotInfo;
  alreadyIndexed: boolean;
}) {
  const stats = snapshot.stats;
  const languages = Object.entries(stats.by_language ?? {}).sort(([, a], [, b]) => b.files - a.files);
  const skipped = Object.entries(stats.skipped ?? {});
  const embedding = stats.embedding;
  const totals: [string, number | undefined][] = [
    ["Files", stats.files],
    ["Chunks", stats.chunks],
    ["Windowed, no parser", stats.by_mode?.windows ?? 0],
    ["Parsed with errors", stats.files_with_parse_errors],
  ];
  const indexed = snapshot.indexed_at
    ? new Date(snapshot.indexed_at).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : null;
  return (
    <section className="panel space-y-6 p-5">
      <div>
        <a
          href={repo.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex max-w-full items-center gap-2 text-base font-semibold text-slate-900 transition-colors hover:text-accent"
        >
          {GITHUB}
          <span className="truncate">
            {repo.owner}/{repo.name}
          </span>
        </a>
        <p className="mt-2">
          <span className="badge bg-slate-50 font-mono text-slate-700 ring-slate-200">
            {snapshot.branch} @ {snapshot.commit_sha?.slice(0, 12) ?? "–"}
          </span>
        </p>
        {indexed && (
          <p className="mt-1.5 text-xs text-slate-500 tabular-nums">
            Indexed {indexed}
            {stats.seconds !== undefined && <> in {Math.round(stats.seconds)} s</>}
          </p>
        )}
        {alreadyIndexed && (
          <p className="mt-2 text-xs text-slate-500">
            Already indexed at this commit; nothing was re-embedded.
          </p>
        )}
      </div>

      {repo.summary && (
        <div>
          <h3 className="caption">About</h3>
          <p className="mt-2 text-sm leading-relaxed whitespace-pre-line text-slate-700">
            {repo.summary}
          </p>
        </div>
      )}

      <dl className="grid grid-cols-2 gap-2">
        {totals.map(([label, value]) => (
          <div key={label} className="flex flex-col-reverse rounded-lg bg-slate-50 px-3 py-2.5 ring-1 ring-slate-100">
            <dt className="text-xs text-slate-500">{label}</dt>
            <dd className="text-lg font-semibold text-slate-900 tabular-nums">{fmt(value)}</dd>
          </div>
        ))}
      </dl>

      {languages.length > 0 && (
        <div>
          <h3 className="caption">Languages</h3>
          <div className="mt-3 flex h-2 gap-px overflow-hidden rounded-full bg-slate-100">
            {languages.map(([language, row], index) => (
              <div
                key={language}
                title={`${language === "text" ? "other" : language}: ${row.files} files`}
                style={{
                  width: `${(row.files / languages.reduce((sum, [, r]) => sum + r.files, 0)) * 100}%`,
                  backgroundColor: languageColor(language, index),
                }}
              />
            ))}
          </div>
          <table className="mt-3 w-full table-fixed text-sm">
            <thead className="text-left text-xs text-slate-400">
              <tr>
                <th className="py-1 font-normal">Language</th>
                <th className="w-[20%] py-1 text-right font-normal">Files</th>
                <th className="w-[22%] py-1 text-right font-normal">Symbols</th>
                <th className="w-[20%] py-1 text-right font-normal">Chunks</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {languages.map(([language, row], index) => (
                <tr key={language} className="border-t border-slate-100">
                  {/* "text" is indexing.py's key for files windowed without a parser. */}
                  <td className="truncate py-1.5 text-slate-700">
                    <span
                      className="mr-2 inline-block size-2 rounded-full align-middle"
                      style={{ backgroundColor: languageColor(language, index) }}
                    />
                    {language === "text" ? "other" : language}
                  </td>
                  <td className="py-1.5 text-right">{fmt(row.files)}</td>
                  <td className="py-1.5 text-right">{fmt(row.symbols)}</td>
                  <td className="py-1.5 text-right">{fmt(row.chunks)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-t border-slate-100 pt-4 text-xs">
        <dt className="text-slate-500">Skipped</dt>
        <dd className="text-slate-700 tabular-nums">
          {skipped.length > 0
            ? skipped.map(([reason, count]) => `${reason} ${count.toLocaleString()}`).join(", ")
            : "none"}
        </dd>
        {embedding && (
          <>
            <dt className="text-slate-500">Embedded</dt>
            <dd className="text-slate-700 tabular-nums">
              {fmt(embedding.embedded)} new · {fmt(embedding.reused)} reused
            </dd>
            <dt className="text-slate-500">Tokens</dt>
            <dd className="text-slate-700 tabular-nums">
              {fmt(embedding.tokens)} · ${embedding.cost_usd.toFixed(4)}
            </dd>
            <dt className="text-slate-500">Model</dt>
            <dd className="font-mono text-slate-700">{embedding.model}</dd>
          </>
        )}
      </dl>
      {stats.summary?.status === "failed" && (
        <p className="text-xs text-slate-500">
          The summary call failed, so there is no summary or suggested questions.
        </p>
      )}
    </section>
  );
}
