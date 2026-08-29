// Оболочка: вход, боковое меню на компьютере, нижнее на телефоне.

import React, { useCallback, useEffect, useState } from 'react';
import { api, ago, NeedsLogin } from './api.js';
import { Btn, Choice, Toast, useTheme } from './ui.jsx';
import Overview from './screens/Overview.jsx';
import Post from './screens/Post.jsx';
import Posts from './screens/Posts.jsx';
import Topics from './screens/Topics.jsx';
import Keys from './screens/Keys.jsx';
import Group from './screens/Group.jsx';
import { Events, Spending } from './screens/Money.jsx';

const NAV = [
  ['overview', 'Обзор'],
  ['posts', 'Посты'],
  ['topics', 'Темы'],
  ['group', 'Группа'],
  ['keys', 'Ключи и доступы'],
  ['spending', 'Расходы'],
  ['events', 'Что происходит'],
];

// На телефоне внизу помещается четыре пункта, остальное — за «Ещё».
const PHONE = ['overview', 'posts', 'topics'];

export default function App() {
  const [ready, setReady] = useState(false);
  const [inside, setInside] = useState(false);
  const [screen, setScreen] = useState({ name: 'overview', params: {} });
  const [slug, setSlug] = useState(null);
  const [summary, setSummary] = useState(null);
  const [toast, setToast] = useState(null);
  const [more, setMore] = useState(false);
  const [theme, flipTheme] = useTheme();

  const say = useCallback((message, bad = false) => setToast({ message, bad }), []);

  useEffect(() => {
    api.session().then(() => setInside(true)).catch(() => setInside(false)).finally(() => setReady(true));
  }, []);

  // Сводка нужна меню: счётчик у «Постов», красная точка у «Ключей», состояние
  // воркера внизу. Обновляется редко — панель работает на слабом железе.
  useEffect(() => {
    if (!inside) return undefined;
    const load = () => api.overview().then((data) => {
      setSummary(data);
      if (!slug && data.groups.length) setSlug(data.groups[0].slug);
    }).catch(() => {});
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, [inside, slug]);

  const go = useCallback((name, params = {}) => {
    setScreen({ name, params });
    setMore(false);
    window.scrollTo(0, 0);
  }, []);

  if (!ready) return null;
  if (!inside) return <Login onIn={() => setInside(true)} />;

  const waiting = (summary?.groups || []).reduce((sum, group) => sum + group.waiting, 0);
  const keysAlarm = (summary?.alerts || []).some(
    (alert) => alert.name === 'vk_token' || alert.name === 'provider_blocked'
  );
  const active = screen.name === 'post' ? 'posts' : screen.name;

  const body = (() => {
    switch (screen.name) {
      case 'post': return <Post id={screen.params.id} onBack={() => go('posts')} onToast={say} />;
      case 'posts': return <Posts initial={screen.params} onOpen={go} onToast={say} />;
      case 'topics': return slug ? <Topics slug={slug} onToast={say} /> : null;
      case 'spending': return <Spending />;
      case 'events': return <Events onOpen={go} />;
      case 'keys': return slug ? <Keys slug={slug} onToast={say} /> : null;
      case 'group': return slug ? <Group slug={slug} onToast={say} /> : null;
      default: return <Overview onOpen={go} />;
    }
  })();

  return (
    <div className="shell">
      <nav className="side">
        <div className="brand">Контент-фабрика</div>
        {NAV.map(([code, label]) => (
          <button key={code} type="button" className="nav" data-on={active === code ? '1' : '0'} onClick={() => go(code)}>
            {label}
            {code === 'posts' && waiting ? <span className="count">{waiting}</span> : null}
            {code === 'keys' && keysAlarm ? <span className="dot s bad" /> : null}
          </button>
        ))}
        <div className="foot">
          <div>
            Проход воркера<br />
            <span style={{ color: summary?.health?.stale ? 'var(--error-text)' : 'var(--muted)' }}>
              {summary ? ago(summary.health.tick_age_sec) : '—'}
            </span>
          </div>
          <button type="button" onClick={flipTheme}>
            {theme === 'dark' ? 'Тёмная тема · переключить' : 'Светлая тема · переключить'}
          </button>
          <button type="button" onClick={() => api.logout().then(() => setInside(false))}>Выйти</button>
        </div>
      </nav>

      <main className="page">{body}</main>

      <nav className="tabs">
        {PHONE.map((code) => (
          <button key={code} type="button" data-on={active === code ? '1' : '0'} onClick={() => go(code)}>
            <span className="box" />
            {NAV.find(([name]) => name === code)[1]}
          </button>
        ))}
        <button type="button" data-on={more || !PHONE.includes(active) ? '1' : '0'} onClick={() => setMore(!more)}>
          <span className="box" />
          Ещё
        </button>
      </nav>

      {more ? (
        <div className="back" onClick={() => setMore(false)}>
          <div className="sheet" onClick={(event) => event.stopPropagation()}>
            <h2>Ещё</h2>
            {NAV.filter(([code]) => !PHONE.includes(code)).map(([code, label]) => (
              <button key={code} type="button" className="btn wide" onClick={() => go(code)}>{label}</button>
            ))}
            <button type="button" className="btn wide" onClick={flipTheme}>
              {theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
            </button>
            <button type="button" className="btn wide plain" onClick={() => api.logout().then(() => setInside(false))}>
              Выйти
            </button>
          </div>
        </div>
      ) : null}

      <Toast message={toast?.message} bad={toast?.bad} onHide={() => setToast(null)} />
    </div>
  );
}

function Login({ onIn }) {
  const [password, setPassword] = useState('');
  const [trusted, setTrusted] = useState(true);
  const [failed, setFailed] = useState(null);

  const enter = async () => {
    setFailed(null);
    try {
      await api.login(password, trusted);
      setPassword('');
      onIn();
    } catch (problem) {
      setFailed(problem instanceof NeedsLogin ? 'Пароль не подошёл.' : problem.message);
      throw problem;
    }
  };

  return (
    <div className="page" style={{ maxWidth: 420, margin: '0 auto', justifyContent: 'center', minHeight: '100vh' }}>
      <div className="col gap-8">
        <div style={{ font: '500 24px/1.25 var(--sans)' }}>Контент-фабрика</div>
        <div className="muted">Панель управления сообществами</div>
      </div>

      <div className="field">
        <span className="kicker">Пароль</span>
        <input
          type="password"
          autoComplete="current-password"
          className={failed ? 'bad' : ''}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter') enter().catch(() => {}); }}
          style={{ height: 52, fontFamily: 'var(--mono)', letterSpacing: '.2em' }}
        />
      </div>

      {failed ? (
        <div className="banner bad">
          <div className="row top" style={{ gap: 10 }}>
            <span className="dot bad" style={{ marginTop: 6 }} />
            <span className="why">{failed}</span>
          </div>
        </div>
      ) : null}

      <Choice kind="main" what="Войти" then="панель управляет живыми публикациями" onRun={enter} busy="Проверяю…" />

      <label className="check">
        <input type="checkbox" checked={trusted} onChange={(event) => setTrusted(event.target.checked)} />
        Не спрашивать на этом устройстве 30 дней
      </label>

      <div className="faint" style={{ borderTop: '1px solid var(--line-4)', paddingTop: 16 }}>
        Пароль задан при установке. Забыли — его меняют на самой машине, где
        стоит фабрика, командой <span className="mono">factory panel-password</span>.
      </div>
    </div>
  );
}
