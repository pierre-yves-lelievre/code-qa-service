import { useState } from "react";

/** A counter button. */
export function Counter() {
  const [n, setN] = useState(0);
  return <button onClick={() => setN(n + 1)}>{n}</button>;
}

export const Label = ({ text }: { text: string }) => <span>{text}</span>;
