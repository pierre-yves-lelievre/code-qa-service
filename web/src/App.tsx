/** The page shell: a header and the main area. */
export default function App() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <header className="mb-6">
        <h1 className="text-xl font-semibold">Code Q&amp;A</h1>
        <p className="text-sm text-slate-500">Ask questions about a public GitHub repository.</p>
      </header>
      <main />
    </div>
  );
}
