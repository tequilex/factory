// Обзор: всё ли в порядке, за две секунды и без чтения.

import React from 'react';
import { api, ago, money, when } from '../api.js';
import { Failed, Loading, useData } from '../ui.jsx';

const ALERT_WORDS = {
  vk_token: 'Истёк ключ загрузки картинок в ВК',
  no_topics: 'Скоро публиковать нечего: кончаются темы',
  stuck: 'Пост висит на одном шаге больше суток',
  failed: 'Пост сломался окончательно',
  provider_blocked: 'Провайдер отказывает: лимит ключа или доступ',
  budget: 'Пост вышел дороже потолка',
  worker_silent: 'Воркер молчит',
};

export default function Overview({ onOpen }) {
  const { loading, data, error, refresh } = useData(() => api.overview(), []);

  // Обзор обновляется сам, но редко: панель работает на одноплатнике, и частый
  // опрос отнимает у воркера то немногое, что у него есть.
  React.useEffect(() => {
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const { health, groups, alerts, broken } = data;

  return (
    <>
      <h1>Обзор</h1>
      <p className="lead">Что происходит прямо сейчас</p>

      {health.stale ? (
        <div className="card alarm">
          <h2>Работа встала</h2>
          <div>Воркер не отвечал {ago(health.tick_age_sec)}.</div>
          <div className="sub mt" style={{ color: 'inherit' }}>
            Пока он молчит, посты не готовятся и не публикуются. Одобренное
            подождёт и уедет, когда он вернётся.
          </div>
        </div>
      ) : (
        <div className="card tight">
          <span className="dot ok" />
          Воркер отработал {ago(health.tick_age_sec)}
        </div>
      )}

      {Object.entries(broken || {}).map(([slug, reason]) => (
        <div className="card alarm" key={slug}>
          <h2>Не читается конфиг: {slug}</h2>
          <div className="mono-block mt">{reason}</div>
        </div>
      ))}

      {alerts.map((alert) => (
        <div className="card notice tight" key={`${alert.name}:${alert.scope}`}>
          <span className="dot warn" />
          {ALERT_WORDS[alert.name] || alert.name}
          <span className="faint"> · {alert.scope}</span>
        </div>
      ))}

      {groups.length === 0 ? (
        <div className="card sub">Ни одной группы не подключено.</div>
      ) : null}

      {groups.map((group) => (
        <div className="card" key={group.slug}>
          <div className="spread">
            <h2>{group.title}</h2>
            <span className="sub">
              <span className={`dot ${group.paused ? 'off' : group.schedule_off ? 'warn' : 'ok'}`} />
              {group.paused ? 'на паузе' : group.schedule_off ? 'расписание выключено' : 'работает'}
            </span>
          </div>

          <button
            type="button"
            className="act ghost"
            style={{ padding: 0, border: 'none', display: 'block', textAlign: 'left', minHeight: 0 }}
            onClick={() => onOpen('posts', { state: 'in_review' })}
          >
            <div className="big num">{group.waiting}</div>
            <div className="sub">ждут вашего решения</div>
          </button>

          <div className="grid2">
            <div className="cell"><div className="kicker">в работе</div><div className="v num">{group.working}</div></div>
            <div className="cell"><div className="kicker">тем в запасе</div><div className="v num">{group.free_topics}</div></div>
            <div className="cell"><div className="kicker">сегодня</div><div className="v num">{money(group.spent_today)}</div></div>
            <div className="cell"><div className="kicker">за месяц</div><div className="v num">{money(group.spent_month)}</div></div>
          </div>

          <div className="cell mt" style={{ borderTop: '1px solid var(--line-soft)', paddingTop: 10 }}>
            <div className="kicker">ближайшая публикация</div>
            <div>
              {group.schedule_off
                ? 'расписание выключено — уйдёт ближайшим проходом'
                : group.next_slot ? when(group.next_slot) : 'слотов в расписании нет'}
            </div>
            <div className="sub">
              сегодня опубликовано {group.published_today} из {group.posts_per_day}
            </div>
          </div>

          {group.failed ? (
            <div className="sub mt" style={{ color: 'var(--error-text)' }}>
              сломалось постов: {group.failed}
            </div>
          ) : null}
        </div>
      ))}
    </>
  );
}
