// Обращения к панели.
//
// Одно место на всё, потому что во всех ответах одинаково важны две вещи:
// 401 означает «покажи вход», а не «ошибка», и текст отказа от сервера уже
// написан по-русски и для человека — придумывать свой поверх него нельзя.

export class NeedsLogin extends Error {}

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });

  if (response.status === 401) throw new NeedsLogin('Нужен вход.');

  if (!response.ok) {
    let detail = `Ответ ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === 'string'
        ? body.detail
        // Ошибка проверки полей приходит списком: показываем первое поле,
        // остальное владельцу не поможет.
        : body.detail.map((item) => item.msg).join('; ');
    } catch { /* ответ без тела — оставляем код */ }
    throw new Error(detail);
  }

  return response.status === 204 ? null : response.json();
}

const json = (body) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  login: (password, trusted) => request('/api/login', json({ password, trusted })),
  logout: () => request('/api/logout', { method: 'POST' }),
  session: () => request('/api/session'),

  overview: () => request('/api/overview'),
  states: () => request('/api/states'),

  posts: (params = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, value]) => value !== null && value !== '')
    );
    return request(`/api/posts?${query}`);
  },
  post: (id) => request(`/api/posts/${id}`),
  imageUrl: (id, position, stamp) => `/api/posts/${id}/image/${position}?v=${stamp || 0}`,

  decide: (id, decision, version) => request(`/api/posts/${id}/decision`, json({ decision, version })),
  editText: (id, body, title) => request(`/api/posts/${id}/text`, json({ body, title })),
  switchVersion: (id, number) => request(`/api/posts/${id}/version/${number}`, { method: 'POST' }),
  redraw: (id, position, prompt) => request(`/api/posts/${id}/redraw/${position}`, json({ prompt })),
  uploadImage: (id, position, file) => {
    const form = new FormData();
    form.append('file', file);
    return request(`/api/posts/${id}/image/${position}`, { method: 'POST', body: form });
  },

  topics: (slug) => request(`/api/topics/${encodeURIComponent(slug)}`),
  addTopics: (slug, text) => request(`/api/topics/${encodeURIComponent(slug)}`, json({ text })),
  reorder: (slug, ids) => request(`/api/topics/${encodeURIComponent(slug)}/order`, {
    ...json({ ids }), method: 'PUT',
  }),

  spending: (params = {}) => request(`/api/spending?${new URLSearchParams(params)}`),
  events: (params = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, value]) => value !== null && value !== '' && value !== false)
    );
    return request(`/api/events?${query}`);
  },

  settings: (slug) => request(`/api/groups/${encodeURIComponent(slug)}/settings`),
  saveSettings: (slug, changes) => request(`/api/groups/${encodeURIComponent(slug)}/settings`, json({ changes })),
  previewSettings: (slug, changes) => request(`/api/groups/${encodeURIComponent(slug)}/settings/preview`, json({ changes })),
  saveFile: (slug, path, text) => request(`/api/groups/${encodeURIComponent(slug)}/file`, json({ path, text })),
  checkAccess: (slug) => request(`/api/groups/${encodeURIComponent(slug)}/check`, { method: 'POST' }),
  previewCharacter: (slug, scene, character) =>
    request(`/api/groups/${encodeURIComponent(slug)}/preview`, json({ scene, character })),

  keys: (slug) => request(`/api/groups/${encodeURIComponent(slug)}/keys`),
  sendVkCode: (slug, text) => request(`/api/groups/${encodeURIComponent(slug)}/vk-code`, json({ text })),
};

// Оформление чисел и времени -------------------------------------------------

export const money = (value) =>
  `${Number(value || 0).toFixed(2).replace('.', ',')} ₽`;

export const when = (iso) => {
  if (!iso) return '—';
  const at = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`);
  return at.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  });
};

export const ago = (seconds) => {
  if (seconds === null || seconds === undefined) return 'ни разу';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return 'меньше минуты назад';
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} ч назад` : `${Math.floor(hours / 24)} дн назад`;
};

// Цвет точки состояния. Значения — коды из State, выдумывать свои нельзя.
export const stateTone = (state) => {
  if (state === 'in_review' || state === 'approved') return 'ok';
  if (state === 'failed') return 'bad';
  if (state === 'published') return 'off';
  if (state === 'rejected') return 'off';
  return 'work';
};
