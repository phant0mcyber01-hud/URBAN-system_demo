// Operator actions. Reading needs no credentials; everything that changes state sends the
// operator token, which is kept for the browser session only.
const KEY = 'urban-operator';
let token = '';
try { token = sessionStorage.getItem(KEY) ?? ''; } catch { /* storage blocked: stay in view mode */ }

const headers = () => ({ 'X-Operator-Token': token, 'Content-Type': 'application/json' });

export async function verify(candidate = token) {
  if (!candidate) return false;
  const r = await fetch('/api/me', { headers: { 'X-Operator-Token': candidate } }).catch(() => null);
  const ok = !!r?.ok;
  token = ok ? candidate : '';
  try { ok ? sessionStorage.setItem(KEY, token) : sessionStorage.removeItem(KEY); } catch { /* ignore */ }
  return ok;
}

export function logout() {
  token = '';
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

export async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: headers(), body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(r.status === 401 ? 'нет прав оператора' : r.status === 409 ? 'действие сейчас невозможно' : `ошибка ${r.status}`);
  return r.json();
}
