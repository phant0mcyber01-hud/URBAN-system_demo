// What a pytest run has to look like for a gate to pass: everything passed AND nothing was skipped.
// A skipped test is a claim that was not checked (a missing optional tool, a platform guard), so a gate that
// counted it as fine would pass on less than it says. Locally nothing is skipped (checked with -rs).
//
// Legitimate skips, if one ever exists, go in ALLOWED_SKIPS with the reason: { match: /text of the skip line/, reason }.
export const ALLOWED_SKIPS = [];

// output: pytest's stdout+stderr of a run made with `-q -rs`; status: its exit code.
// Returns { ok, passed, message } - message says why not, or what was allowed.
export function judge(output, status) {
  const lines = output.trim().split(/\r?\n/);
  const summary = lines[lines.length - 1];
  const passed = /(\d+) passed/.exec(summary);
  if (status !== 0 || !passed || /\b(failed|errors?)\b/.test(summary)) return { ok: false, message: "pytest: " + summary };
  const counted = Number((/(\d+) skipped/.exec(summary) || [0, 0])[1]);
  const listed = [...output.matchAll(/^SKIPPED \[(\d+)\] (.+)$/gm)].map((m) => ({ n: Number(m[1]), text: m[2] }));
  const listedCount = listed.reduce((a, s) => a + s.n, 0);
  if (counted !== listedCount) return { ok: false, message: `${counted} test(s) skipped but ${listedCount} accounted for in the -rs list: ${summary}` };
  const unallowed = listed.filter((s) => !ALLOWED_SKIPS.some((a) => a.match.test(s.text)));
  if (unallowed.length) {
    return { ok: false, message: `${counted} test(s) skipped, a skipped test is an unchecked claim:\n  ` +
      unallowed.map((s) => `${s.n} x ${s.text}`).join("\n  ") };
  }
  const note = listed.length ? ` (allowed skips: ${listed.map((s) => s.text).join("; ")})` : "";
  return { ok: true, passed: passed[1], message: summary + note };
}
