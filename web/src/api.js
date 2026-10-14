// Session with the platform. The bearer token comes from /api/login and is kept for the browser
// tab only; the server forgets it after 12 hours, on restart, or when the account is disabled.
const KEY = 'urban-session';
let token = '';
try { token = sessionStorage.getItem(KEY) ?? ''; } catch { /* storage blocked: sign in each time */ }

const remember = (t) => {
  token = t;
  try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY); } catch { /* ignore */ }
};
export const wsToken = () => token;

const ERRORS = {
  400: 'проверьте данные: имя до 40 символов (буквы, цифры, . - _), пароль от 10 символов, текущий пароль верный',
  401: 'нужно войти заново', 403: 'недостаточно прав', 404: 'не найдено',
  409: 'действие сейчас невозможно', 429: 'слишком много попыток, подождите минуту',
};

async function call(method, path, body) {
  const r = await fetch(path, {
    method, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) remember('');
  if (!r.ok) throw new Error(ERRORS[r.status] ?? `ошибка ${r.status}`);
  return r.json();
}
export const get = (path) => call('GET', path);
export const post = (path, body) => call('POST', path, body);

/** The signed-in account {name, role}, or null when the stored token is gone or stale. */
export async function whoami() {
  if (!token) return null;
  try { return await get('/api/me'); } catch { return null; }
}

export async function login(name, password) {
  let s;
  try { s = await post('/api/login', { name, password }); } catch (e) {
    throw new Error(e.message === ERRORS[401] ? 'неверное имя или пароль' : e.message);
  }
  remember(s.token);
  return { name: s.name, role: s.role };
}

export async function logout() {
  try { await post('/api/logout'); } catch { /* the server may already have forgotten us */ }
  remember('');
}
