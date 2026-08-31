export function exitCodeFor(results) {
  return results.some((result) => !result.ok) ? 1 : 0;
}
