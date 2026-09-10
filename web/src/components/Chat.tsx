import { type FormEvent, type KeyboardEvent, useState } from "react";
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
  "space-y-2 text-sm leading-relaxed [&_blockquote]:border-l-2 [&_blockquote]:pl-3 " +
  "[&_blockquote]:text-slate-600 [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 " +
  "[&_code]:font-mono [&_code]:text-[0.85em] [&_h1]:font-semibold [&_h2]:font-semibold " +
  "[&_h3]:font-semibold [&_h4]:font-semibold [&_ol]:list-decimal [&_ol]:pl-5 " +
  "[&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-slate-100 [&_pre]:p-3 " +
  "[&_pre_code]:px-0 [&_ul]:list-disc [&_ul]:pl-5";

const PANEL = "rounded-lg border border-slate-200 bg-white p-4";

interface Turn {
  question: string;
  response: AskResponse | null;
  error: string | null;
}

/** One answer: the markdown, a not-found badge, notes, then its sources and trace. */
function Answer({ response }: { response: AskResponse }) {
  return (
    <div>
      {response.not_found && (
        <span className="mb-2 inline-block rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
          Not found in this repository
        </span>
      )}
      <div className={MARKDOWN_STYLE}>
        <Markdown skipHtml allowedElements={ALLOWED_ELEMENTS} unwrapDisallowed>
          {response.answer}
        </Markdown>
      </div>
      {response.notes.length > 0 && (
        <ul className="mt-2 text-xs text-slate-500">
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

/** Questions and answers about one repository; the conversation lives in this state only. */
export default function Chat({ repo }: { repo: RepoResponse }) {
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
    <section className="space-y-4">
      {repo.summary && (
        <div className={PANEL}>
          <h2 className="font-medium">About this repository</h2>
          <p className="mt-1 text-sm whitespace-pre-line text-slate-700">{repo.summary}</p>
        </div>
      )}

      <div className={`${PANEL} space-y-4`}>
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Chat</h2>
          {turns.length > 0 && (
            <button
              onClick={newChat}
              disabled={busy}
              className="text-sm text-blue-700 hover:underline disabled:opacity-50"
            >
              New chat
            </button>
          )}
        </div>

        {turns.length === 0 && suggested.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {suggested.map((question) => (
              <button
                key={question}
                onClick={() => ask(question)}
                disabled={busy}
                className="rounded-full border border-slate-300 px-3 py-1 text-left text-sm hover:bg-slate-50 disabled:opacity-50"
              >
                {question}
              </button>
            ))}
          </div>
        )}

        <ol className="space-y-6">
          {turns.map((turn, index) => (
            <li key={index} className="space-y-2">
              <p className="rounded bg-slate-100 px-3 py-2 text-sm whitespace-pre-wrap">
                {turn.question}
              </p>
              {turn.response ? (
                <Answer response={turn.response} />
              ) : turn.error ? (
                <p className="text-sm text-red-700">{turn.error}</p>
              ) : (
                <p className="text-sm text-slate-500">Answering…</p>
              )}
            </li>
          ))}
        </ol>

        <form onSubmit={submit} className="flex items-end gap-2">
          <textarea
            value={draft}
            maxLength={MAX_QUESTION}
            rows={2}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            disabled={busy}
            placeholder={turns.length ? "Ask a follow-up…" : "Ask about the code…"}
            className="w-full resize-y rounded border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy || !draft.trim()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {busy ? "Answering…" : "Ask"}
          </button>
        </form>
      </div>
    </section>
  );
}
