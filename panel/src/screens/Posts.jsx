// Посты: таблица на компьютере, те же строки колонкой на телефоне.

import React, { useState } from 'react';
import { api, money, stateTone, when } from '../api.js';
import { Btn, Failed, Loading, useData } from '../ui.jsx';

// Чипы-счётчики: сколько постов в каждом состоянии. Порядок — по тому, как
// часто владелец сюда заглядывает, а не по ходу конвейера.
const CHIPS = [
  ['in_review', 'на просмотре'],
  ['approved', 'одобрены'],
  ['failed', 'сломались'],
  ['published', 'опубликованы'],
  ['rejected', 'выброшены'],
];

export default function Posts({ initial, onOpen, onToast }) {
  const [state, setState] = useState(initial?.state || '');
  const all = useData(() => api.posts({ limit: 200 }), []);
  const { loading, data, error, refresh } = useData(() => api.posts({ state, limit: 100 }), [state]);

  if (error) return <Failed error={error} onRetry={refresh} />;

  const counts = {};
  (all.data || []).forEach((post) => { counts[post.state] = (counts[post.state] || 0) + 1; });
  const waiting = counts.in_review || 0;

  return (
    <>
      <div className="head">
        <div>
          <h1>Посты</h1>
          <div className="under">
            {(all.data || []).length} постов · {waiting} ждут решения
          </div>
        </div>
        <div className="tools">
          <Btn kind="plain" onRun={async () => { refresh(); all.refresh(); }} done="Обновлено">Обновить</Btn>
        </div>
      </div>

      <div className="row wrap" style={{ gap: 8 }}>
        <button
          type="button"
          className="btn"
          style={state === '' ? { borderColor: 'var(--accent)', color: 'var(--accent-2)' } : null}
          onClick={() => setState('')}
        >
          все · {(all.data || []).length}
        </button>
        {CHIPS.map(([code, label]) => (
          <button
            key={code}
            type="button"
            className={`btn ${code === 'failed' && counts[code] ? 'bad' : ''}`}
            style={state === code ? { borderColor: 'var(--accent)', color: 'var(--accent-2)' } : null}
            onClick={() => setState(code)}
          >
            {label} · {counts[code] || 0}
          </button>
        ))}
      </div>

      {loading && !data ? <Loading /> : null}
      {data && data.length === 0 ? <div className="card muted">Здесь пусто.</div> : null}

      {data && data.length ? (
        <div className="table">
          <div className="th">
            <span />
            <span>заголовок</span>
            <span>состояние</span>
            <span>цена</span>
            <span>когда</span>
          </div>

          {data.map((post) => (
            <React.Fragment key={post.id}>
              <button type="button" className="tr" onClick={() => onOpen('post', { id: post.id })}>
                {post.has_cover ? (
                  <img className="thumb" src={api.imageUrl(post.id, 0)} alt="" loading="lazy" />
                ) : (
                  <span className="thumb" />
                )}
                <span style={{ font: '400 14px/1.35 var(--sans)' }}>{post.title || 'без заголовка'}</span>
                <span
                  className="row hide-s"
                  style={{ gap: 7, font: '400 13px/1 var(--sans)', color: 'var(--text-2)' }}
                >
                  <span className={`dot s ${stateTone(post.state)}`} />
                  {post.state_label}
                </span>
                <span className="mono hide-s" style={{ font: '400 13px/1 var(--mono)' }}>{money(post.cost)}</span>
                <span className="hide-s muted">{when(post.created_at)}</span>
              </button>

              {post.state === 'failed' && post.last_error ? (
                <div className="broken">
                  <div className="why">
                    <span className="grow" style={{ font: '400 13px/1.5 var(--sans)', color: 'var(--error-text-3)' }}>
                      {post.last_error}
                    </span>
                    <Btn
                      kind="line none"
                      done="Вернул в работу"
                      onRun={async () => {
                        try {
                          const answer = await api.decide(post.id, 'fix');
                          onToast(answer.what_next);
                          refresh();
                        } catch (problem) { onToast(problem.message, true); throw problem; }
                      }}
                    >
                      Попробовать снова
                    </Btn>
                  </div>
                </div>
              ) : null}
            </React.Fragment>
          ))}
        </div>
      ) : null}
    </>
  );
}
