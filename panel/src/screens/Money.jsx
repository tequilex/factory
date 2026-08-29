// Расходы и лента событий. Один график и одна лента — больше не нужно.

import React, { useState } from 'react';
import { api, money, when } from '../api.js';
import { Btn, Failed, Loading, useData } from '../ui.jsx';

export function Spending() {
  const [days, setDays] = useState(30);
  const { loading, data, error, refresh } = useData(() => api.spending({ days }), [days]);

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const peak = Math.max(
    1,
    ...data.days.map((day) => day.text + day.factcheck + day.images + day.other)
  );

  return (
    <>
      <div className="head"><div><h1>Расходы</h1><div className="under">Картинки — почти вся цена поста</div></div></div>

      <div className="row wrap" style={{ marginBottom: 14 }}>
        {[7, 30, 90].map((value) => (
          <button
            key={value}
            type="button"
            className="btn"
            onClick={() => setDays(value)}
          >
            {value} дней
          </button>
        ))}
      </div>

      <div className="card">
        <div className="stats" style={{ marginTop: 0 }}>
          <div className="cell" style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className="kicker">всего за период</div>
            <div className="v">{money(data.total)}</div>
          </div>
          <div className="cell" style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className="kicker">средняя цена поста</div>
            <div className="v">{data.average_post === null ? '—' : money(data.average_post)}</div>
          </div>
        </div>

        {data.days.length === 0 ? (
          <div className="muted">За этот период трат не было.</div>
        ) : (
          <>
            <div className="chart">
              {data.days.map((day) => {
                const total = day.text + day.factcheck + day.images + day.other;
                const height = (value) => `${(value / peak) * 100}%`;
                return (
                  <div className="day" key={day.day} title={`${day.day}: ${money(total)}`}>
                    <div className="seg images" style={{ height: height(day.images) }} />
                    <div className="seg factcheck" style={{ height: height(day.factcheck) }} />
                    <div className="seg text" style={{ height: height(day.text + day.other) }} />
                  </div>
                );
              })}
            </div>
            <div className="row wrap muted">
              <span><span className="dot ok" />картинки</span>
              <span><span className="dot warn" />проверка фактов</span>
              <span><span className="dot work" />тексты</span>
            </div>
          </>
        )}
      </div>

      <div className="card">
        <div className="kicker">по дням</div>
        <div className="rows">
          {[...data.days].reverse().map((day) => {
            const total = day.text + day.factcheck + day.images + day.other;
            return (
              <div className="rowcard" key={day.day}>
                <div className="grow mono">{day.day}</div>
                <div className="muted">
                  тексты {money(day.text + day.other)} · фактчек {money(day.factcheck)} · картинки {money(day.images)}
                </div>
                <div className="mono">{money(total)}</div>
              </div>
            );
          })}
        </div>
        {data.days.length === 0 ? null : (
          <div className="faint">
            Пустой день означает, что система в этот день ничего не готовила:
            стояла на паузе, ждала тем или упиралась в истёкший ключ.
          </div>
        )}
      </div>
    </>
  );
}

export function Events({ onOpen }) {
  const [onlyErrors, setOnlyErrors] = useState(false);
  const { loading, data, error, refresh } = useData(
    () => api.events({ only_errors: onlyErrors, limit: 80 }),
    [onlyErrors]
  );

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  return (
    <>
      <div className="head"><div><h1>Что происходит</h1><div className="under">Последние действия системы</div></div></div>

      <label className="check" style={{ marginBottom: 14 }}>
        <input type="checkbox" checked={onlyErrors} onChange={(event) => setOnlyErrors(event.target.checked)} />
        Только ошибки
      </label>

      {data.length === 0 ? <div className="card muted">Записей нет.</div> : null}

      <div className="rows">
        {data.map((event, index) => (
          <div className="rowcard" key={index}>
            <span className="faint mono" style={{ width: 92, flex: 'none' }}>{when(event.at)}</span>
            <span className={`dot ${event.ok ? 'ok' : 'bad'}`} />
            <div className="grow">
              <div>
                {event.post_id ? (
                  <button
                    type="button"
                    onClick={() => onOpen('post', { id: event.post_id })}
                    style={{ background: 'none', border: 'none', color: 'var(--accent)', padding: 0, cursor: 'pointer', font: 'inherit' }}
                  >
                    Пост {event.post_id}
                  </button>
                ) : 'Система'}{' — '}{event.step_label}
              </div>
              {event.error ? (
                <div className="muted" style={{ color: 'var(--error-text)', whiteSpace: 'pre-wrap' }}>
                  {event.error}
                </div>
              ) : null}
            </div>
            {event.cost ? <span className="faint mono">{money(event.cost)}</span> : null}
          </div>
        ))}
      </div>
    </>
  );
}
