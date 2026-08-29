// Общие части интерфейса.

import React, { useCallback, useEffect, useRef, useState } from 'react';

export function useTheme() {
  const [theme, setTheme] = useState(() => {
    const saved = localStorage.getItem('factory-theme');
    if (saved) return saved;
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('factory-theme', theme);
  }, [theme]);

  return [theme, () => setTheme(theme === 'dark' ? 'light' : 'dark')];
}

/**
 * Решение: заголовок и последствие под ним.
 *
 * Три состояния — можно нажать, выполняется, принято — и все три видны. Система
 * работает сама и медленно: «Опубликовать» не публикует, а ставит отметку.
 * Кнопка, мгновенно возвращающаяся в исходное, обещает то, чего не произошло.
 */
export function Choice({ what, then, busy = 'Принято, выполняется', done, kind = '', onRun, disabled }) {
  const [state, setState] = useState('idle');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const run = async () => {
    setState('busy');
    try {
      await onRun();
      if (alive.current) setState('done');
      setTimeout(() => alive.current && setState('idle'), 3000);
    } catch {
      if (alive.current) setState('idle');
    }
  };

  const label = state === 'busy' ? busy : state === 'done' ? (done || busy) : what;
  const under = state === 'idle' ? then : state === 'busy' ? 'воркер подхватит ближайшим проходом' : null;

  return (
    <button
      type="button"
      className={`choice ${kind} ${state !== 'idle' ? 'busy' : ''}`}
      disabled={disabled || state !== 'idle'}
      onClick={run}
    >
      <span className="what">{label}</span>
      {under ? <span className="then">{under}</span> : null}
    </button>
  );
}

/** Обычная кнопка с тем же правилом трёх состояний, но без подписи снизу. */
export function Btn({ children, onRun, kind = '', done = 'Готово', disabled, ...rest }) {
  const [state, setState] = useState('idle');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  if (!onRun) {
    return <button type="button" className={`btn ${kind}`} disabled={disabled} {...rest}>{children}</button>;
  }

  const run = async () => {
    setState('busy');
    try {
      await onRun();
      if (alive.current) setState('done');
      setTimeout(() => alive.current && setState('idle'), 2500);
    } catch {
      if (alive.current) setState('idle');
    }
  };

  return (
    <button
      type="button"
      className={`btn ${kind}`}
      disabled={disabled || state !== 'idle'}
      onClick={run}
      {...rest}
    >
      {state === 'busy' ? 'Выполняется…' : state === 'done' ? done : children}
    </button>
  );
}

export function Toast({ message, bad, onHide }) {
  useEffect(() => {
    if (!message) return undefined;
    const timer = setTimeout(onHide, bad ? 9000 : 5000);
    return () => clearTimeout(timer);
  }, [message, bad, onHide]);

  if (!message) return null;
  return <div className={`toast ${bad ? 'bad' : ''}`} onClick={onHide}>{message}</div>;
}

export function Sheet({ title, children, onClose }) {
  return (
    <div className="back" onClick={onClose}>
      <div className="sheet" onClick={(event) => event.stopPropagation()}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function useData(load, deps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const refresh = useCallback(() => {
    let alive = true;
    setState((old) => ({ ...old, loading: true }));
    load()
      .then((data) => alive && setState({ loading: false, data, error: null }))
      .catch((error) => alive && setState({ loading: false, data: null, error }));
    return () => { alive = false; };
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(refresh, [refresh]);
  return { ...state, refresh };
}

export function Loading({ what = 'Загружаю…' }) {
  return <div className="card muted">{what}</div>;
}

export function Failed({ error, onRetry }) {
  return (
    <div className="banner bad">
      <div className="grow">
        <div className="head-line">Не получилось загрузить</div>
        <div className="why" style={{ whiteSpace: 'pre-wrap' }}>{String(error.message)}</div>
      </div>
      {onRetry ? <button type="button" className="btn bad none" onClick={onRetry}>Ещё раз</button> : null}
    </div>
  );
}
