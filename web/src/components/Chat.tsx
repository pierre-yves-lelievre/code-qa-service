import { type FormEvent, type KeyboardEvent, type ReactNode, useState } from "react";
import Markdown from "react-markdown";
import { api, type AskResponse, errorText, type RepoResponse } from "../api";
import Sources from "./Sources";
import Trace from "./Trace";

const MAX_QUESTION = 2000;

// Answers are markdown from the model. Raw HTML is skipped and only these elements are kept:
// `img` is dropped, so no remote image is fetched, and `a` is unwrapped to its text, so no URL
// written by the model is ever rendered as a link.
const ALLOWED_ELEMENTS = [
  "p",
  "br",
  "strong",
  "em",
  "code",
  "pre",
  "ul",
  "ol",
  "li",
  "blockquote",
  "h1",
  "h2",
  "h3",
  "h4",
  "hr",
];

const MARKDOWN_STYLE =
  "max-w-prose space-y-3 text-sm leading-relaxed text-slate-800 " +
  "[&_blockquote]:border-l-2 [&_blockquote]:border-slate-300 [&_blockquote]:pl-3 " +
  "[&_blockquote]:text-slate-600 [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 " +
  "[&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.85em] [&_h1]:text-base " +
  "[&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:font-semibold " +
  "[&_h4]:font-semibold [&_hr]:border-slate-200 [&_li]:mt-1 [&_ol]:list-decimal [&_ol]:pl-5 " +
  "[&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-slate-50 [&_pre]:p-3 " +
  "[&_pre]:leading-relaxed [&_pre]:ring-1 [&_pre]:ring-slate-200 [&_pre_code]:bg-transparent " +
  "[&_pre_code]:p-0 [&_strong]:font-semibold [&_strong]:text-slate-900 [&_ul]:list-disc " +
  "[&_ul]:pl-5";

const ICON = "size-4 shrink-0";

// A suggested question, in the sidebar and in the empty chat.
const SUGGESTION =
  "flex h-full w-full items-start gap-2.5 rounded-lg bg-white px-3 py-2.5 text-left text-sm " +
  "text-slate-700 ring-1 ring-slate-200 transition-colors hover:bg-accent-soft hover:text-slate-900 " +
  "hover:ring-indigo-200 focus-visible:outline-2 focus-visible:outline-accent " +
  "disabled:pointer-events-none disabled:opacity-50";

// The header's mark (App.tsx), smaller: it stands for the assistant.
const MARK = (
  <svg viewBox="0 0 32 32" className="size-7 shrink-0" aria-hidden="true">
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

const SEND = (
  <svg
    viewBox="0 0 24 24"
    className={ICON}
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

const SPINNER = (
  <svg
    viewBox="0 0 24 24"
    className={`${ICON} animate-spin`}
    fill="none"
    stroke="currentColor"
    strokeWidth="2.5"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="9" className="opacity-25" />
    <path d="M21 12a9 9 0 0 0-9-9" strokeLinecap="round" />
  </svg>
);

const SPARKLE = (
  <svg viewBox="0 0 24 24" className={`${ICON} mt-0.5 text-accent`} fill="currentColor" aria-hidden="true">
    <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
  </svg>
);

interface Turn {
  question: string;
  response: AskResponse | null;
  error: string | null;
}

/** One answer: a not-found badge, the markdown, notes, then its sources and trace. */
function Answer({ response }: { response: AskResponse }) {
  return (
    <div className="space-y-4">
      {response.not_found && (
        <span className="badge bg-slate-100 text-slate-700 ring-slate-300">
          Not found in this repository
        </span>
      )}
      <div className={MARKDOWN_STYLE}>
        <Markdown skipHtml allowedElements={ALLOWED_ELEMENTS} unwrapDisallowed>
          {response.answer}
        </Markdown>
      </div>
      {response.notes.length > 0 && (
        <ul className="space-y-1 text-xs text-slate-500">
          {response.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <Sources sources={response.sources} />
      <Trace response={response} />
    </div>
  );
}

/** The placeholder while a question is being answered. */
function Pending() {
  return (
    <div className="animate-pulse space-y-2.5 pt-1" role="status">
      <p className="text-sm text-slate-500">Answering…</p>
      <div className="h-2 w-4/5 rounded-full bg-slate-200" />
      <div className="h-2 w-3/5 rounded-full bg-slate-200" />
      <div className="h-2 w-2/5 rounded-full bg-slate-200" />
    </div>
  );
}

/** The repository sidebar with the suggested questions, and the conversation, kept in this state only. */
export default function Chat({ repo, aside }: { repo: RepoResponse; aside: ReactNode }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const suggested = repo.suggested_questions ?? [];

  /** Send one question, continuing the conversation when there is one. */
  async function ask(text: string) {
    const question = text.trim();
    if (!question || busy) return;
    const index = turns.length;
    setBusy(true);
    setDraft("");
    setTurns((previous) => [...previous, { question, response: null, error: null }]);
    const update = (change: Partial<Turn>) =>
      setTurns((previous) => previous.map((turn, i) => (i === index ? { ...turn, ...change } : turn)));
    try {
      const response = await api<AskResponse>("POST", "/ask", {
        repo_id: repo.repo_id,
        question,
        conversation_id: conversationId,
      });
      setConversationId(response.conversation_id);
      update({ response });
    } catch (error) {
      update({ error: errorText(error) });
    }
    setBusy(false);
  }

  /** Drop the conversation: the next question starts a new one. */
  function newChat() {
    setTurns([]);
    setConversationId(null);
  }

  /** Ask the drafted question. */
  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  /** Enter sends; Shift+Enter adds a line. */
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void ask(draft);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)] lg:items-start xl:grid-cols-[22rem_minmax(0,1fr)]">
      <aside className="space-y-6 lg:sticky lg:top-24 lg:-mx-1 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto lg:px-1 lg:pb-1">
        {aside}
        {suggested.length > 0 && (
          // Below lg the empty chat shows the same questions right under this panel.
          <section className={`panel p-5 ${turns.length === 0 ? "hidden lg:block" : ""}`}>
            <h2 className="caption">Suggested questions</h2>
            <ul className="mt-3 space-y-2">
              {suggested.map((question) => (
                <li key={question}>
                  <button onClick={() => ask(question)} disabled={busy} className={SUGGESTION}>
                    {SPARKLE}
                    <span>{question}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </aside>

      <section className="panel flex min-h-[36rem] flex-col">
        <div className="flex items-center justify-between gap-4 border-b border-slate-100 px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">Chat</h2>
            <p className="truncate text-xs text-slate-500">
              About {repo.owner}/{repo.name}
            </p>
          </div>
          {turns.length > 0 && (
            <button onClick={newChat} disabled={busy} className="btn-secondary">
              New chat
            </button>
          )}
        </div>

        <div className="flex-1 px-5 py-6">
          {turns.length === 0 ? (
            <div className="flex h-full min-h-64 flex-col items-center justify-center text-center">
              {MARK}
              <p className="mt-3 text-sm font-medium text-slate-900">
                Ask about {repo.owner}/{repo.name}
              </p>
              <p className="mt-1 max-w-sm text-sm text-slate-500">
                Answers cite the files and lines they come from. Try one of these, or write your own
                below.
              </p>
              {suggested.length > 0 && (
                <ul className="mt-6 grid w-full max-w-2xl gap-2 text-left sm:grid-cols-2">
                  {suggested.map((question) => (
                    <li key={question}>
                      <button onClick={() => ask(question)} disabled={busy} className={SUGGESTION}>
                        {SPARKLE}
                        <span>{question}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <ol className="space-y-10">
              {turns.map((turn, index) => (
                <li key={index} className="space-y-4">
                  <div className="flex justify-end">
                    <p className="max-w-[85%] rounded-2xl rounded-br-md bg-accent-soft px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-900 ring-1 ring-indigo-100">
                      {turn.question}
                    </p>
                  </div>
                  <div className="flex gap-3">
                    {MARK}
                    <div className="min-w-0 flex-1">
                      {turn.response ? (
                        <Answer response={turn.response} />
                      ) : turn.error ? (
                        <p className="error-note">{turn.error}</p>
                      ) : (
                        <Pending />
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>

        <form
          onSubmit={submit}
          className="sticky bottom-0 rounded-b-xl border-t border-slate-100 bg-white/95 px-5 py-4 backdrop-blur"
        >
          <div
            className={`flex items-end gap-2 rounded-xl p-1.5 pl-3 shadow-sm ring-1 ring-slate-300 transition focus-within:ring-2 focus-within:ring-accent ${busy ? "animate-pulse bg-slate-50" : "bg-white"}`}
          >
            <label htmlFor="question" className="sr-only">
              Question
            </label>
            <textarea
              id="question"
              value={draft}
              maxLength={MAX_QUESTION}
              rows={1}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              disabled={busy}
              placeholder={turns.length ? "Ask a follow-up…" : "Ask about the code…"}
              className="field-sizing-content max-h-40 min-h-9 w-full resize-none bg-transparent py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed"
            />
            <button
              type="submit"
              disabled={busy || !draft.trim()}
              aria-label={busy ? "Answering" : "Ask"}
              className="btn-primary size-9 shrink-0 p-0"
            >
              {busy ? SPINNER : SEND}
            </button>
          </div>
          <p className="mt-2 text-xs text-slate-400">
            {busy ? "Answering…" : "Enter to send · Shift+Enter for a new line"}
          </p>
        </form>
      </section>
    </div>
  );
}
