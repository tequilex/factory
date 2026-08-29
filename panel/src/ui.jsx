// Мелкие общие части: тема, сообщения, диалог, кнопка с тремя состояниями.

import React, { useCallback, useEffect, useRef, useState } from 'react';

/** Тёмная или светлая. Системная настройка уважается, выбор запоминается. */
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
 * Кнопка действия с тремя состояниями: можно нажать → выполняется → сделано.
 *
 * Главное правило интерфейса: система работает сама и медленно, «Опубликовать»
 * не публикует, а ставит отметку. Кнопка, возвращающаяся в исходное состояние
 * мгновенно, обещает то, чего не произошло.
 */
export function Action({ onRun, children, price, kind = '', done = 'Принято', disabled, ...rest }) {
  const [state, setState] = useState('idle');
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const run = useCallback(async () => {
    setState('busy');
    try {
      await onRun();
      if (alive.current) setState('done');
      setTimeout(() => alive.current && setState('idle'), 2500);
    } catch (error) {
      if (alive.current) setState('idle');
      throw error;
    }
  }, [onRun]);

  return (
    <button
      type="button"
      className={`act ${kind}`}
      // Собственное состояние и внешний запрет складываются. Раньше внешний
      // disabled перекрывал своё, и на время выполнения кнопка снова
      // становилась нажимаемой — двойное нажатие уходило дважды.
      disabled={disabled || state !== 'idle'}
      onClick={() => run().catch(() => {})}
      {...rest}
    >
      {state === 'busy' ? 'Выполняется…' : state === 'done' ? done : children}
      {state === 'idle' && price ? <span className="price">{price}</span> : null}
    </button>
  );
}

/** Сообщение внизу экрана. Текст берётся от сервера как есть. */
export function Toast({ message, bad, onHide }) {
  useEffect(() => {
    if (!message) return undefined;
    // Ошибки висят дольше: их читают, а подтверждения только замечают.
    const timer = setTimeout(onHide, bad ? 9000 : 5000);
    return () => clearTimeout(timer);
  }, [message, bad, onHide]);

  if (!message) return null;
  return <div className={`toast ${bad ? 'bad' : ''}`} onClick={onHide}>{message}</div>;
}

export function Modal({ title, children, onClose }) {
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

/** Данные с сервера: загрузка, ошибка, повтор. */
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
  return <div className="card sub">{what}</div>;
}

export function Failed({ error, onRetry }) {
  return (
    <div className="card alarm">
      <h2>Не получилось загрузить</h2>
      <div className="sub" style={{ color: 'inherit', whiteSpace: 'pre-wrap' }}>{String(error.message)}</div>
      {onRetry ? <button type="button" className="act ghost mt" onClick={onRetry}>Попробовать снова</button> : null}
    </div>
  );
}
