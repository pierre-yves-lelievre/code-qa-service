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

const STAGES: Record<string, string> = {
  cloning: "Cloning the repository",
  walking: "Listing files",
  parsing: "Parsing and chunking",
  embedding: "Embedding chunks",
  summarizing: "Writing the summary",
  done: "Done",
};

const PANEL = "rounded-lg border border-slate-200 bg-white p-4";

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

/** The running job: stage, counts and a bar, polled every POLL_MS. */
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
  const fraction = progressFraction(progress);
  return (
    <section className={PANEL}>
      <h2 className="font-medium">
        Indexing {job.owner}/{job.name} <span className="text-slate-500">@ {job.branch}</span>
      </h2>
      {status === "failed" ? (
        <p className="mt-2 text-sm text-red-700">{state?.error ?? "The index job failed."}</p>
      ) : (
        <>
          <p className="mt-2 text-sm">
            {status === "pending"
              ? "Waiting to start…"
              : (STAGES[progress.stage ?? ""] ?? progress.stage)}
          </p>
          <p className="text-sm text-slate-500">{progressCounts(progress)}</p>
          {fraction !== null && (
            <div className="mt-2 h-2 rounded bg-slate-100">
              <div
                className="h-2 rounded bg-slate-900 transition-all"
                style={{ width: `${Math.round(fraction * 100)}%` }}
              />
            </div>
          )}
        </>
      )}
      {error && <p className="mt-2 text-sm text-red-700">{error}</p>}
      {(status === "failed" || error) && (
        <button onClick={onBack} className="mt-3 text-sm text-blue-700 hover:underline">
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

/** The active snapshot: commit, totals, per-language breakdown, skipped and unparsed files. */
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
  return (
    <section className={PANEL}>
      <h2 className="font-medium">
        Index of {repo.owner}/{repo.name}
      </h2>
      <p className="text-sm text-slate-500">
        {snapshot.branch} @ <code>{snapshot.commit_sha?.slice(0, 12) ?? "–"}</code>
        {snapshot.indexed_at && <> · {new Date(snapshot.indexed_at).toLocaleString()}</>}
        {stats.seconds !== undefined && <> · {stats.seconds} s</>}
      </p>
      {alreadyIndexed && (
        <p className="mt-2 text-sm text-slate-600">
          Already indexed at this commit; nothing was re-embedded.
        </p>
      )}

      <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {totals.map(([label, value]) => (
          <div key={label} className="rounded bg-slate-50 p-2">
            <dt className="text-xs text-slate-500">{label}</dt>
            <dd className="font-medium">{fmt(value)}</dd>
          </div>
        ))}
      </dl>

      {languages.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr>
                <th className="py-1 font-normal">Language</th>
                <th className="py-1 text-right font-normal">Files</th>
                <th className="py-1 text-right font-normal">Symbols</th>
                <th className="py-1 text-right font-normal">Chunks</th>
              </tr>
            </thead>
            <tbody>
              {languages.map(([language, row]) => (
                <tr key={language} className="border-t border-slate-100">
                  <td className="py-1">{language}</td>
                  <td className="py-1 text-right">{fmt(row.files)}</td>
                  <td className="py-1 text-right">{fmt(row.symbols)}</td>
                  <td className="py-1 text-right">{fmt(row.chunks)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-3 text-sm text-slate-600">
        Skipped:{" "}
        {skipped.length > 0
          ? skipped.map(([reason, count]) => `${reason} ${count.toLocaleString()}`).join(", ")
          : "none"}
      </p>
      {embedding && (
        <p className="text-sm text-slate-600">
          Embedding ({embedding.model}): {fmt(embedding.embedded)} new, {fmt(embedding.reused)}{" "}
          reused, {fmt(embedding.tokens)} tokens, ${embedding.cost_usd.toFixed(4)}
        </p>
      )}
      {stats.summary?.status === "failed" && (
        <p className="text-sm text-slate-600">
          The summary call failed, so there is no summary or suggested questions.
        </p>
      )}
    </section>
  );
}
