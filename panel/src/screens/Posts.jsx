// Список постов. На компьютере таблица, на телефоне карточки — но и там, и
// там строка одна и та же, просто перестроенная: два разных представления
// разъехались бы при первой же правке.

import React, { useState } from 'react';
import { api, money, stateTone, when } from '../api.js';
import { Failed, Loading, useData } from '../ui.jsx';

export default function Posts({ initial, onOpen }) {
  const [state, setState] = useState(initial?.state || '');
  const states = useData(() => api.states(), []);
  const { loading, data, error, refresh } = useData(() => api.posts({ state }), [state]);

  if (error) return <Failed error={error} onRetry={refresh} />;

  return (
    <>
      <h1>Посты</h1>
      <p className="lead">Свежие сверху</p>

      <div className="row wrap" style={{ marginBottom: 14 }}>
        <button
          type="button"
          className={`act small ${state === '' ? '' : 'ghost'}`}
          onClick={() => setState('')}
        >
          Все
        </button>
        {['in_review', 'approved', 'failed', 'published'].map((code) => (
          <button
            key={code}
            type="button"
            className={`act small ${state === code ? '' : 'ghost'}`}
            onClick={() => setState(code)}
          >
            {states.data?.[code] || code}
          </button>
        ))}
      </div>

      {loading && !data ? <Loading /> : null}

      {data && data.length === 0 ? (
        <div className="card sub">Здесь пусто.</div>
      ) : null}

      <div className="list">
        {(data || []).map((post) => (
          <div className="item" key={post.id}>
            {post.has_cover ? (
              <img
                src={api.imageUrl(post.id, 0)}
                alt=""
                width={48}
                height={60}
                loading="lazy"
                style={{ borderRadius: 6, objectFit: 'cover', flex: 'none' }}
              />
            ) : (
              <div style={{ width: 48, height: 60, borderRadius: 6, background: 'var(--panel-soft)', flex: 'none' }} />
            )}

            <button
              type="button"
              className="grow"
              onClick={() => onOpen('post', { id: post.id })}
              style={{ background: 'none', border: 'none', color: 'inherit', textAlign: 'left', cursor: 'pointer', padding: 0 }}
            >
              <div className="title">{post.title || 'без заголовка'}</div>
              <div className="sub">
                <span className={`dot ${stateTone(post.state)}`} />
                {post.state_label} · {money(post.cost)} · {when(post.created_at)}
              </div>
              {post.state === 'failed' && post.last_error ? (
                <div className="sub" style={{ color: 'var(--error-text)' }}>
                  {post.last_error.split('\n')[0]}
                </div>
              ) : null}
            </button>

            {post.external_id ? (
              <a
                className="act small ghost"
                href={`https://vk.com/wall${post.external_id}`}
                target="_blank"
                rel="noreferrer"
              >
                В ВК
              </a>
            ) : null}
          </div>
        ))}
      </div>
    </>
  );
}
