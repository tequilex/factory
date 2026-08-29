// Обзор: «всё ли в порядке» за две секунды, без чтения.

import React, { useEffect } from 'react';
import { api, money, when } from '../api.js';
import { Btn, Failed, Loading, useData } from '../ui.jsx';

// Тревога человеческим языком: короткое название и что именно случилось.
// Кнопка ведёт туда, где чинят, — иначе тревога только пугает.
const ALERTS = {
  vk_token: { name: 'Истёк ключ загрузки', what: 'картинки не загружаются в ВК, публикация стоит', go: ['keys', 'Обновить ключ'], tone: 'bad' },
  no_topics: { name: 'Кончаются темы', what: 'скоро создавать посты будет не из чего', go: ['topics', 'Добавить темы'], tone: 'warn' },
  stuck: { name: 'Пост завис', what: 'висит на одном шаге больше суток', go: ['posts', 'Открыть посты'], tone: 'warn' },
  failed: { name: 'Пост сломался', what: 'пять попыток не удались', go: ['posts', 'Открыть посты'], tone: 'bad' },
  provider_blocked: { name: 'Провайдер отказывает', what: 'исчерпан лимит ключа или отозван доступ', go: ['keys', 'Ключи'], tone: 'bad' },
  budget: { name: 'Пост дороже потолка', what: 'остановлен, чтобы не жечь деньги', go: ['posts', 'Открыть посты'], tone: 'warn' },
  worker_silent: { name: 'Воркер молчит', what: 'посты не двигаются', go: null, tone: 'bad' },
};

export default function Overview({ onOpen }) {
  const { loading, data, error, refresh } = useData(() => api.overview(), []);

  useEffect(() => {
    // Редко: панель работает на одноплатнике, и частый опрос отнимает у воркера
    // то немногое, что у него есть.
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const { groups, alerts, broken } = data;
  const now = new Date();

  return (
    <>
      <div className="head">
        <div>
          <h1>Обзор</h1>
          <div className="under">
            {now.toLocaleDateString('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' })},{' '}
            {now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}
          </div>
        </div>
        <div className="tools">
          <Btn kind="plain" onRun={async () => refresh()} done="Обновлено">Обновить</Btn>
        </div>
      </div>

      {Object.entries(broken || {}).map(([slug, reason]) => (
        <div className="banner bad" key={slug}>
          <div className="grow">
            <div className="head-line"><span className="dot bad" />Не читается конфиг группы {slug}</div>
            <div className="pre" style={{ marginTop: 8 }}>{reason}</div>
          </div>
        </div>
      ))}

      {alerts.length ? (
        <div className="col gap-8">
          <div className="kicker">Тревоги · {alerts.length}</div>
          {alerts.map((alert) => {
            const known = ALERTS[alert.name] || { name: alert.name, what: '', go: null, tone: 'warn' };
            return (
              <div className="rowcard" key={`${alert.name}:${alert.scope}`}>
                <span className={`dot ${known.tone === 'bad' ? 'bad' : 'warn'}`} />
                <span className="name">{known.name}</span>
                <span className="grow muted">{alert.scope}{known.what ? ` · ${known.what}` : ''}</span>
                {known.go ? (
                  <button type="button" className="btn line none" onClick={() => onOpen(known.go[0])}>
                    {known.go[1]}
                  </button>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      <div className="col gap-12">
        <div className="kicker">Группы · {groups.length}</div>
        {groups.length === 0 ? <div className="card muted">Ни одной группы не подключено.</div> : null}

        <div className="groups">
          {groups.map((group) => (
            <div className="group" key={group.slug}>
              <div className="spread">
                <div className="col gap-6">
                  <span style={{ font: '500 16px/1.2 var(--sans)' }}>{group.title}</span>
                  <Status paused={group.paused} scheduleOff={group.schedule_off} />
                </div>
                <button
                  type="button"
                  onClick={() => onOpen('posts', { state: 'in_review' })}
                  style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
                >
                  <div className={`count ${group.waiting ? '' : 'zero'}`}>{group.waiting}</div>
                  <div style={{ font: '400 12px/1.2 var(--sans)', color: 'var(--muted)', marginTop: 4 }}>
                    ждут решения
                  </div>
                </button>
              </div>

              <div className="stats">
                <div><div className="v">{group.working}</div><div className="k">в работе</div></div>
                <div>
                  <div className="v" style={group.free_topics <= 6 ? { color: 'var(--warn)' } : null}>
                    {group.free_topics}
                  </div>
                  <div className="k">тем в запасе</div>
                </div>
                <div><div className="v">{money(group.spent_today)}</div><div className="k">сегодня</div></div>
                <div><div className="v">{money(group.spent_month)}</div><div className="k">за месяц</div></div>
              </div>

              <div className="last">{nextLine(group)}</div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

function Status({ paused, scheduleOff }) {
  if (paused) {
    return (
      <span className="row" style={{ gap: 6, font: '400 12px/1 var(--sans)', color: 'var(--muted)' }}>
        <span className="dot s off" />на паузе
      </span>
    );
  }
  if (scheduleOff) {
    return (
      <span className="row" style={{ gap: 6, font: '400 12px/1 var(--sans)', color: 'var(--warn)' }}>
        <span className="dot s warn" />расписание выключено
      </span>
    );
  }
  return (
    <span className="row" style={{ gap: 6, font: '400 12px/1 var(--sans)', color: 'var(--accent-2)' }}>
      <span className="dot s ok" />работает
    </span>
  );
}

function nextLine(group) {
  if (group.paused) return 'На паузе. Ни новых постов, ни публикаций.';
  if (group.schedule_off) {
    return 'Публикует сразу, слотов не ждёт. Одобренный пост уйдёт ближайшим проходом.';
  }
  if (!group.next_slot) return 'В расписании нет слотов — публиковать некогда.';
  return (
    <>
      Ближайшая публикация <b>{when(group.next_slot)}</b>
      <br />
      сегодня опубликовано {group.published_today} из {group.posts_per_day}
    </>
  );
}
