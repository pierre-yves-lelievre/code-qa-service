export function before(): number {
  return 1;
}

function broken() {
  const x = ;
}

export function after(): number {
  return 2;
}
