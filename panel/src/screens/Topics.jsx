// Темы: три списка и порядок очереди.
//
// Порядок меняется стрелками, а не перетаскиванием. Перетаскивание на телефоне
// требует своей библиотеки и на слабом железе заметно тормозит, а стрелки
// работают одинаково везде и не врут: список сразу показывает то, что запишется.

import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { Action, Failed, Loading, useData } from '../ui.jsx';

export default function Topics({ slug, onToast }) {
  const { loading, data, error, refresh } = useData(() => api.topics(slug), [slug]);
  const [tab, setTab] = useState('free');
  const [order, setOrder] = useState([]);
  const [adding, setAdding] = useState('');

  useEffect(() => { if (data) setOrder(data.upcoming); }, [data]);

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const moved = data && order.map((item) => item.id).join() !== data.upcoming.map((item) => item.id).join();

  const move = (index, step) => {
    const next = [...order];
    const target = index + step;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setOrder(next);
  };

  const parsed = adding.split('\n').map((line) => line.trim()).filter(Boolean);

  return (
    <>
      <h1>Темы</h1>
      <p className="lead">
        {data.free} в запасе
        {data.days_left ? ` · при текущей скорости хватит на ${data.days_left} дн` : ''}
      </p>

      {data.free === 0 ? (
        <div className="card notice">
          Свободных тем нет — новые посты создавать не из чего.
        </div>
      ) : null}

      <div className="row wrap" style={{ marginBottom: 14 }}>
        {[['free', `в запасе · ${data.free}`], ['taken', `в работе · ${data.taken}`], ['used', `отработано · ${data.used}`]].map(
          ([code, label]) => (
            <button
              key={code}
              type="button"
              className={`act small ${tab === code ? '' : 'ghost'}`}
              onClick={() => setTab(code)}
            >
              {label}
            </button>
          )
        )}
      </div>

      {tab === 'free' ? (
        <>
          <div className="list">
            {order.map((topic, index) => (
              <div className="item" key={topic.id}>
                <span className="faint num" style={{ width: 22 }}>{index + 1}</span>
                <div className="grow title">{topic.title}</div>
                <button type="button" className="act small ghost" disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
                <button type="button" className="act small ghost" disabled={index === order.length - 1} onClick={() => move(index, 1)}>↓</button>
              </div>
            ))}
          </div>

          {moved ? (
            <div className="card mt">
              <div className="sub">Порядок изменён, но ещё не сохранён.</div>
              <div className="row mt">
                <Action
                  kind="main"
                  onRun={async () => {
                    try {
                      const answer = await api.reorder(slug, order.map((item) => item.id));
                      onToast(answer.what_next);
                      refresh();
                    } catch (problem) { onToast(problem.message, true); throw problem; }
                  }}
                >
                  Сохранить порядок
                </Action>
                <button type="button" className="act ghost" onClick={() => setOrder(data.upcoming)}>
                  Вернуть как было
                </button>
              </div>
            </div>
          ) : null}

          <div className="card mt">
            <h2>Добавить темы</h2>
            <div className="sub">По одной в строке. Добавление ничего не стоит.</div>
            <textarea
              className="mt"
              rows={6}
              value={adding}
              placeholder={'Зимняя резина: когда пора переобуваться\nЧто будет, если пропустить одно ТО'}
              onChange={(event) => setAdding(event.target.value)}
            />
            <div className="sub mt">Распознано тем: {parsed.length}</div>
            <Action
              kind="main mt"
              disabled={parsed.length === 0}
              onRun={async () => {
                try {
                  const answer = await api.addTopics(slug, adding);
                  onToast(answer.what_next);
                  setAdding('');
                  refresh();
                } catch (problem) { onToast(problem.message, true); throw problem; }
              }}
            >
              {parsed.length ? `Добавить ${parsed.length} в конец очереди` : 'Вставьте темы'}
            </Action>
          </div>
        </>
      ) : null}

      {tab === 'taken' ? (
        <div className="list">
          {data.in_progress.map((topic, index) => (
            <div className="item" key={index}>
              <div className="grow">
                <div className="title">{topic.title}</div>
                <div className="sub">{topic.note}</div>
              </div>
            </div>
          ))}
          {data.in_progress.length === 0 ? <div className="card sub">Ничего не готовится.</div> : null}
        </div>
      ) : null}

      {tab === 'used' ? (
        <div className="list">
          {data.done.map((topic, index) => (
            <div className="item" key={index}>
              <div className="grow">
                <div className="title">{topic.title}</div>
                <div className="sub">{topic.note}</div>
              </div>
              {topic.url ? (
                <a className="act small ghost" href={topic.url} target="_blank" rel="noreferrer">В ВК</a>
              ) : null}
            </div>
          ))}
          {data.done.length === 0 ? <div className="card sub">Пока ничего не отработано.</div> : null}
        </div>
      ) : null}
    </>
  );
}
