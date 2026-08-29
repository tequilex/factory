// Расходы и лента событий. Один график и одна лента — больше не нужно.

import React, { useState } from 'react';
import { api, money, when } from '../api.js';
import { Failed, Loading, useData } from '../ui.jsx';

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
      <h1>Расходы</h1>
      <p className="lead">Картинки — почти вся цена поста</p>

      <div className="row wrap" style={{ marginBottom: 14 }}>
        {[7, 30, 90].map((value) => (
          <button
            key={value}
            type="button"
            className={`act small ${days === value ? '' : 'ghost'}`}
            onClick={() => setDays(value)}
          >
            {value} дней
          </button>
        ))}
      </div>

      <div className="card">
        <div className="grid2" style={{ marginTop: 0 }}>
          <div className="cell" style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className="kicker">всего за период</div>
            <div className="v num">{money(data.total)}</div>
          </div>
          <div className="cell" style={{ borderTop: 'none', paddingTop: 0 }}>
            <div className="kicker">средняя цена поста</div>
            <div className="v num">{data.average_post === null ? '—' : money(data.average_post)}</div>
          </div>
        </div>

        {data.days.length === 0 ? (
          <div className="sub mt">За этот период трат не было.</div>
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
            <div className="row wrap sub">
              <span><span className="dot ok" />картинки</span>
              <span><span className="dot warn" />проверка фактов</span>
              <span><span className="dot work" />тексты</span>
            </div>
          </>
        )}
      </div>

      <div className="card">
        <div className="kicker">по дням</div>
        <div className="list">
          {[...data.days].reverse().map((day) => {
            const total = day.text + day.factcheck + day.images + day.other;
            return (
              <div className="item" key={day.day}>
                <div className="grow num">{day.day}</div>
                <div className="sub num">
                  тексты {money(day.text + day.other)} · фактчек {money(day.factcheck)} · картинки {money(day.images)}
                </div>
                <div className="num">{money(total)}</div>
              </div>
            );
          })}
        </div>
        {data.days.length === 0 ? null : (
          <div className="faint mt">
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
      <h1>Что происходит</h1>
      <p className="lead">Последние действия системы</p>

      <label className="check" style={{ marginBottom: 14 }}>
        <input type="checkbox" checked={onlyErrors} onChange={(event) => setOnlyErrors(event.target.checked)} />
        Только ошибки
      </label>

      {data.length === 0 ? <div className="card sub">Записей нет.</div> : null}

      <div className="list">
        {data.map((event, index) => (
          <div className="item" key={index}>
            <span className="faint num" style={{ width: 92, flex: 'none' }}>{when(event.at)}</span>
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
                <div className="sub" style={{ color: 'var(--error-text)', whiteSpace: 'pre-wrap' }}>
                  {event.error}
                </div>
              ) : null}
            </div>
            {event.cost ? <span className="faint num">{money(event.cost)}</span> : null}
          </div>
        ))}
      </div>
    </>
  );
}
