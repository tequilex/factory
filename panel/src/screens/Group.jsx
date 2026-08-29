// Настройки группы. Одна длинная форма по разделам, а не мастер из пяти шагов:
// её заполняют один раз и потом правят по частям.

import React, { useEffect, useState } from 'react';
import { api, money } from '../api.js';
import { Action, Failed, Loading, useData } from '../ui.jsx';

export default function Group({ slug, onToast }) {
  const { loading, data, error, refresh } = useData(() => api.settings(slug), [slug]);
  const [draft, setDraft] = useState(null);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => { if (data) setDraft(structuredClone(data.values)); }, [data]);

  if (loading && !data) return <Loading />;
  if (error) return <Failed error={error} onRetry={refresh} />;
  if (!draft) return <Loading />;

  const set = (section, field, value) =>
    setDraft({ ...draft, [section]: { ...draft[section], [field]: value } });

  const save = (section) => async () => {
    try {
      const answer = await api.saveSettings(slug, { [section]: draft[section] });
      onToast(answer.what_next);
      refresh();
    } catch (problem) { onToast(problem.message, true); throw problem; }
  };

  const image = draft.image || {};
  const limits = draft.limits || {};
  const llm = draft.llm || {};
  const vk = draft.vk || {};
  const perPost =
    (Number(image.price_per_image) || 0) * ((Number(image.inline_count) || 0) + 1);

  return (
    <>
      <h1>Группа {slug}</h1>
      <p className="lead">Воркер перечитывает настройки каждый проход</p>

      <div className="card">
        <h2>Сообщество</h2>
        <div className="sub">Номер группы ВКонтакте: <span className="num">{vk.group_id}</span></div>
        <a className="act ghost mt" href={`https://vk.com/club${vk.group_id}`} target="_blank" rel="noreferrer">
          Открыть в ВК
        </a>
      </div>

      <div className="card">
        <h2>Расписание</h2>
        <div className="sub">Во сколько выходят посты. Слотов должно быть не меньше, чем публикаций в сутки.</div>
        <div className="row wrap mt">
          {(vk.schedule || []).map((slot, index) => (
            <span key={index} className="act small ghost" style={{ cursor: 'default' }}>
              {slot}
              <button
                type="button"
                onClick={() => set('vk', 'schedule', vk.schedule.filter((_, i) => i !== index))}
                style={{ background: 'none', border: 'none', color: 'var(--faint)', cursor: 'pointer', padding: 0 }}
              >
                ✕
              </button>
            </span>
          ))}
          <AddSlot onAdd={(value) => set('vk', 'schedule', [...(vk.schedule || []), value].sort())} />
        </div>
        <div className="faint mt">Часовой пояс: {vk.timezone}</div>
        <Action kind="ghost mt" onRun={save('vk')} done="Сохранено">Сохранить расписание</Action>
      </div>

      <div className="card">
        <h2>Лимиты</h2>
        <label className="field">
          <span className="name">публикаций в сутки</span>
          <input
            type="number" min="1" value={limits.posts_per_day ?? ''}
            onChange={(event) => set('limits', 'posts_per_day', Number(event.target.value))}
          />
        </label>
        <label className="field">
          <span className="name">держать в работе (не меньше публикаций в сутки)</span>
          <input
            type="number" min="1" value={limits.queue_buffer ?? ''}
            onChange={(event) => set('limits', 'queue_buffer', Number(event.target.value))}
          />
        </label>
        <label className="field">
          <span className="name">потолок стоимости поста, ₽ — дороже пост останавливается</span>
          <input
            type="number" step="0.5" min="0.1" value={limits.max_cost_per_post ?? ''}
            onChange={(event) => set('limits', 'max_cost_per_post', Number(event.target.value))}
          />
        </label>
        <Action kind="ghost" onRun={save('limits')} done="Сохранено">Сохранить лимиты</Action>
      </div>

      <div className="card">
        <h2>Модели</h2>
        <label className="field">
          <span className="name">пишет тексты</span>
          <input value={llm.model || ''} onChange={(event) => set('llm', 'model', event.target.value)} />
        </label>
        <label className="field">
          <span className="name">проверяет факты — обязательно с поиском в интернете</span>
          <input value={llm.factcheck_model || ''} onChange={(event) => set('llm', 'factcheck_model', event.target.value)} />
        </label>
        <label className="field">
          <span className="name">рисует картинки — имя с организацией впереди</span>
          <input value={image.model || ''} onChange={(event) => set('image', 'model', event.target.value)} />
        </label>
        <label className="field">
          <span className="name">цена картинки, ₽</span>
          <input
            type="number" step="0.01" value={image.price_per_image ?? ''}
            onChange={(event) => set('image', 'price_per_image', Number(event.target.value))}
          />
        </label>
        <div className="card tight">
          Примерно <b className="num">{money(perPost)}</b> за пост картинками
          при {(Number(image.inline_count) || 0) + 1} картинках. Текст и проверка
          фактов добавляют копейки — картинки почти вся цена.
        </div>
        <div className="row mt">
          <Action kind="ghost" onRun={save('llm')} done="Сохранено">Сохранить модели текста</Action>
          <Action kind="ghost" onRun={save('image')} done="Сохранено">Сохранить модель картинок</Action>
        </div>
      </div>

      <div className="card">
        <h2>Персонаж</h2>
        <label className="field">
          <span className="name">приметы по-английски: уходят в модель дословно, в начало каждого промпта</span>
          <textarea
            rows={4} value={image.character || ''}
            onChange={(event) => set('image', 'character', event.target.value)}
          />
        </label>
        <div className="card tight">
          Держатся: черты лица, стрижка, цвет глаз, пирсинг, серьги.
          Не держится: подробный рисунок татуировки — он перерисовывается каждый
          раз. Приметы, которых модель не держит, обещают постоянство, которого
          не будет.
        </div>
        <label className="field mt">
          <span className="name">стиль съёмки</span>
          <input value={image.scene_style || ''} onChange={(event) => set('image', 'scene_style', event.target.value)} />
        </label>
        <Action kind="ghost" onRun={save('image')} done="Сохранено">Сохранить персонажа</Action>
        <TryCharacter slug={slug} character={image.character} onToast={onToast} />
      </div>

      <div className="card">
        <h2>Файл настроек целиком</h2>
        <div className="sub">Для тех, кто хочет посмотреть, что получилось.</div>
        <button type="button" className="act ghost mt" onClick={() => setShowRaw(!showRaw)}>
          {showRaw ? 'Свернуть' : 'Показать'}
        </button>
        {showRaw ? <div className="mono-block mt">{data.raw}</div> : null}
      </div>
    </>
  );
}

function AddSlot({ onAdd }) {
  const [value, setValue] = useState('');
  return (
    <span className="row">
      <input
        value={value}
        placeholder="19:30"
        style={{ width: 92 }}
        onChange={(event) => setValue(event.target.value)}
      />
      <button
        type="button"
        className="act small ghost"
        disabled={!/^\d{2}:\d{2}$/.test(value)}
        onClick={() => { onAdd(value); setValue(''); }}
      >
        Добавить
      </button>
    </span>
  );
}

/** Одна пробная картинка: подбирать внешность целым постом впятеро дороже. */
function TryCharacter({ slug, character, onToast }) {
  const [scene, setScene] = useState('standing next to a car, daylight, documentary photo');
  const [result, setResult] = useState(null);

  return (
    <div className="card tight mt">
      <div className="kicker">проверить внешность</div>
      <label className="field">
        <span className="name">сцена по-английски, без примет</span>
        <input value={scene} onChange={(event) => setScene(event.target.value)} />
      </label>
      <Action
        kind="ghost"
        price={money(1.68)}
        done="Нарисовано"
        onRun={async () => {
          try {
            setResult(await api.previewCharacter(slug, scene, character));
          } catch (problem) { onToast(problem.message, true); throw problem; }
        }}
      >
        Нарисовать пробную
      </Action>
      {result ? (
        <>
          <img src={result.image} alt="Проба персонажа" className="mt" style={{ width: '100%', borderRadius: 12 }} />
          <div className="mono-block mt">{result.prompt}</div>
          {result.cost ? <div className="faint mt">Стоило {money(result.cost)}</div> : null}
        </>
      ) : null}
    </div>
  );
}
