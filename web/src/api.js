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
  if (r.status === 401 && path !== '/api/login') remember('');
  if (!r.ok) {
    const err = new Error(ERRORS[r.status] ?? `ошибка ${r.status}`);
    err.status = r.status;
    try { err.detail = (await r.json()).detail; } catch { /* no body */ }
    if (typeof err.detail === 'string' && /[а-яё]/i.test(err.detail)) err.message = err.detail;  // already in words for people
    throw err;
  }
  return r.json();
}
export const get = (path) => call('GET', path);
export const post = (path, body) => call('POST', path, body);

/** The signed-in account {name, role}, or null when the stored token is gone or stale. */
export async function whoami() {
  if (!token) return null;
  try { return await get('/api/me'); } catch { return null; }
}

/** Sign in. Throws an error with .needCode when the account has a second factor and no code was given. */
export async function login(name, password, code = '') {
  let s;
  try { s = await post('/api/login', { name, password, code }); } catch (e) {
    if (e.detail === 'code required') {
      const need = new Error('введите код из приложения-аутентификатора');
      need.needCode = true;
      throw need;
    }
    throw new Error(e.status === 401 ? (code ? 'неверное имя, пароль или код' : 'неверное имя или пароль') : e.message);
  }
  remember(s.token);
  return { name: s.name, role: s.role, mfa: s.mfa, mfa_setup: s.mfa_setup };
}

export async function logout() {
  try { await post('/api/logout'); } catch { /* the server may already have forgotten us */ }
  remember('');
}
