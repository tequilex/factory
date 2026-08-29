// Оболочка: вход, навигация, переключение темы.

import React, { useCallback, useEffect, useState } from 'react';
import { api, NeedsLogin } from './api.js';
import { Action, Toast, useTheme } from './ui.jsx';
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
  ['events', 'События'],
  ['spending', 'Расходы'],
  ['group', 'Группа'],
  ['keys', 'Ключи'],
];

// На телефоне внизу помещается четыре пункта, остальное — за «Ещё».
const PHONE_NAV = ['overview', 'posts', 'topics'];

export default function App() {
  const [ready, setReady] = useState(false);
  const [inside, setInside] = useState(false);
  const [screen, setScreen] = useState({ name: 'overview', params: {} });
  const [slug, setSlug] = useState(null);
  const [toast, setToast] = useState(null);
  const [more, setMore] = useState(false);
  const [theme, flipTheme] = useTheme();

  const say = useCallback((message, bad = false) => setToast({ message, bad }), []);

  useEffect(() => {
    api.session()
      .then(() => setInside(true))
      .catch(() => setInside(false))
      .finally(() => setReady(true));
  }, []);

  // Группа нужна экранам тем, ключей и настроек. Берётся из обзора, чтобы не
  // заводить отдельный список: пока группа одна, выбирать не из чего.
  useEffect(() => {
    if (!inside || slug) return;
    api.overview().then((data) => {
      if (data.groups.length) setSlug(data.groups[0].slug);
    }).catch(() => {});
  }, [inside, slug]);

  const go = useCallback((name, params = {}) => {
    setScreen({ name, params });
    setMore(false);
    window.scrollTo(0, 0);
  }, []);

  if (!ready) return null;
  if (!inside) return <Login onIn={() => setInside(true)} onToast={say} />;

  const body = (() => {
    switch (screen.name) {
      case 'post': return <Post id={screen.params.id} onBack={() => go('posts')} onToast={say} />;
      case 'posts': return <Posts initial={screen.params} onOpen={go} />;
      case 'topics': return slug ? <Topics slug={slug} onToast={say} /> : null;
      case 'spending': return <Spending />;
      case 'events': return <Events onOpen={go} />;
      case 'keys': return slug ? <Keys slug={slug} onToast={say} /> : null;
      case 'group': return slug ? <Group slug={slug} onToast={say} /> : null;
      default: return <Overview onOpen={go} />;
    }
  })();

  const active = screen.name === 'post' ? 'posts' : screen.name;

  return (
    <div className="shell">
      <nav className="side">
        <div className="brand">Контент-фабрика</div>
        {NAV.map(([code, label]) => (
          <button key={code} type="button" data-on={active === code ? '1' : '0'} onClick={() => go(code)}>
            {label}
          </button>
        ))}
        <button type="button" onClick={flipTheme}>
          {theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
        </button>
        <button type="button" onClick={() => api.logout().then(() => setInside(false))}>Выйти</button>
      </nav>

      <main className="page">{body}</main>

      <nav className="tabs">
        {PHONE_NAV.map((code) => {
          const label = NAV.find(([name]) => name === code)[1];
          return (
            <button key={code} type="button" data-on={active === code ? '1' : '0'} onClick={() => go(code)}>
              <span className="mark" />
              {label}
            </button>
          );
        })}
        <button type="button" data-on={more || !PHONE_NAV.includes(active) ? '1' : '0'} onClick={() => setMore(!more)}>
          <span className="mark" />
          Ещё
        </button>
      </nav>

      {more ? (
        <div className="modal-back" onClick={() => setMore(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h2>Ещё</h2>
            <div className="stack">
              {NAV.filter(([code]) => !PHONE_NAV.includes(code)).map(([code, label]) => (
                <button key={code} type="button" className="act ghost" onClick={() => go(code)}>{label}</button>
              ))}
              <button type="button" className="act ghost" onClick={flipTheme}>
                {theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
              </button>
              <button type="button" className="act ghost" onClick={() => api.logout().then(() => setInside(false))}>
                Выйти
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <Toast message={toast?.message} bad={toast?.bad} onHide={() => setToast(null)} />
    </div>
  );
}

function Login({ onIn, onToast }) {
  const [password, setPassword] = useState('');
  const [trusted, setTrusted] = useState(false);
  const [failed, setFailed] = useState(null);

  const enter = async () => {
    setFailed(null);
    try {
      await api.login(password, trusted);
      setPassword('');
      onIn();
    } catch (problem) {
      // NeedsLogin здесь означает просто неверный пароль: показываем это в
      // форме, а не общим сообщением внизу экрана.
      const message = problem instanceof NeedsLogin ? 'Пароль не подошёл.' : problem.message;
      setFailed(message);
      throw problem;
    }
  };

  return (
    <div className="page" style={{ maxWidth: 420, paddingTop: 60 }}>
      <h1>Панель контент-фабрики</h1>
      <p className="lead">Управление публикациями в ваши сообщества</p>
      <div className="card">
        <label className="field">
          <span className="name">пароль</span>
          <input
            type="password"
            autoComplete="current-password"
            className={failed ? 'bad' : ''}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') enter().catch(() => {}); }}
          />
        </label>
        <label className="check">
          <input type="checkbox" checked={trusted} onChange={(event) => setTrusted(event.target.checked)} />
          Не спрашивать на этом устройстве 30 дней
        </label>
        <Action kind="main mt" done="Входим…" onRun={enter}>Войти</Action>
        {failed ? <div className="card alarm tight mt">{failed}</div> : null}
      </div>
      <p className="faint">
        Пароль меняется на самой машине командой
        <span className="num"> factory panel-password</span>.
      </p>
    </div>
  );
}
