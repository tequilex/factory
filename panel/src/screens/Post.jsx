// Просмотр поста — главный экран. Проектируется от телефона.

import React, { useEffect, useState } from 'react';
import { api, money, when } from '../api.js';
import { Action, Failed, Loading, Modal, useData } from '../ui.jsx';

const FACTCHECK_WORDS = {
  fixed: 'Фактчек исправил текст',
  uncertain: 'Фактчек не уверен',
};

// Что делает каждое решение и во что обходится. Цена показывается ДО нажатия:
// «Текст заново» стоит четырёх новых картинок, и владелец должен знать это
// заранее, а не увидеть в расходах послезавтра.
const DECISIONS = [
  { code: 'ok', label: 'Опубликовать', kind: 'main', note: 'уйдёт в группу ближайшей публикацией' },
  { code: 'img', label: 'Картинки заново', note: 'те же сцены, другие кадры', costs: 4 },
  { code: 'scn', label: 'Другие сцены', note: 'текст остаётся', costs: 4 },
  { code: 'txt', label: 'Текст заново', note: 'и новые картинки', costs: 4, warn: true },
];

export default function Post({ id, onBack, onToast }) {
  const { loading, data, error, refresh } = useData(() => api.post(id), [id]);
  const [shown, setShown] = useState(0);
  const [stamp, setStamp] = useState(() => Date.now());
  const [editing, setEditing] = useState(null);
  const [asking, setAsking] = useState(false);
  const [promptFor, setPromptFor] = useState(null);

  useEffect(() => { setShown(0); setStamp(Date.now()); }, [id]);

  if (loading && !data) return <Loading what="Открываю пост…" />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const post = data;
  const ready = post.assets.filter((asset) => asset.ready);
  const current = ready[Math.min(shown, Math.max(ready.length - 1, 0))];
  const perImage = 1.68;

  const after = (answer) => {
    onToast(answer.what_next);
    setStamp(Date.now());
    refresh();
  };

  const act = (code) => async () => {
    try {
      after(await api.decide(post.id, code, post.version));
    } catch (problem) {
      onToast(problem.message, true);
      refresh();
      throw problem;
    }
  };

  return (
    <>
      <button type="button" className="act ghost small" onClick={onBack}>← К списку</button>

      <div className="spread mt">
        <h1 style={{ fontSize: 20 }}>{post.title || 'Без заголовка'}</h1>
      </div>
      <p className="lead">
        <span className={`dot ${post.state === 'in_review' ? 'ok' : 'work'}`} />
        {post.state_label} · {post.project} · {money(post.cost)}
      </p>

      {ready.length ? (
        <>
          <div className="gallery">
            <img
              src={api.imageUrl(post.id, current.position, stamp)}
              alt={current.kind === 'cover' ? 'Обложка' : `Картинка ${current.position}`}
              loading="lazy"
            />
            {current.replaced_by_owner ? <span className="own">ваша</span> : null}
          </div>
          <div className="thumbs">
            {ready.map((asset, index) => (
              <button
                key={asset.position}
                type="button"
                data-on={index === shown ? '1' : '0'}
                onClick={() => setShown(index)}
              >
                <img src={api.imageUrl(post.id, asset.position, stamp)} alt="" loading="lazy" />
              </button>
            ))}
          </div>

          <div className="card mt tight">
            <div className="kicker">промпт этой картинки</div>
            <div className="mono-block">{current.prompt || '—'}</div>
            <div className="sub mt">
              Начало промпта — приметы персонажа из настроек группы.
            </div>
            <div className="row wrap mt">
              <Action
                kind="small ghost"
                price={`${money(perImage)}`}
                onRun={async () => {
                  try {
                    after(await api.redraw(post.id, current.position, null));
                  } catch (problem) { onToast(problem.message, true); throw problem; }
                }}
              >
                Перерисовать эту
              </Action>
              <button type="button" className="act small ghost" onClick={() => setPromptFor(current)}>
                Изменить промпт
              </button>
              <label className="act small ghost" style={{ cursor: 'pointer' }}>
                Поставить свою
                <input
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={async (event) => {
                    const file = event.target.files?.[0];
                    event.target.value = '';
                    if (!file) return;
                    try {
                      after(await api.uploadImage(post.id, current.position, file));
                    } catch (problem) { onToast(problem.message, true); }
                  }}
                />
              </label>
            </div>
          </div>
        </>
      ) : (
        <div className="card sub">Картинок пока нет — они ещё рисуются.</div>
      )}

      {post.factcheck_verdict && FACTCHECK_WORDS[post.factcheck_verdict] ? (
        <div className="card notice">
          <h2 style={{ color: 'inherit' }}>{FACTCHECK_WORDS[post.factcheck_verdict]}</h2>
          <div className="sub" style={{ color: 'inherit' }}>{post.factcheck_notes}</div>
        </div>
      ) : null}

      <div className="card">
        <div className="spread">
          <div className="kicker">заголовок</div>
          <button type="button" className="act small ghost" onClick={() => setEditing('title')}>
            Изменить
          </button>
        </div>
        <div>{post.title}</div>
        <div className="faint mt">
          Печатается на обложке. Смена заголовка пересоберёт её — около минуты,
          картинки не меняются, денег не стоит.
        </div>
      </div>

      <div className="card">
        <div className="spread">
          <div className="kicker">текст поста</div>
          <button type="button" className="act small ghost" onClick={() => setEditing('body')}>
            Изменить
          </button>
        </div>
        <div style={{ whiteSpace: 'pre-wrap' }}>{post.body}</div>
        {post.question ? (
          <div className="mt" style={{ borderTop: '1px solid var(--line-soft)', paddingTop: 10 }}>
            <div className="kicker">вопрос-хук</div>
            {post.question}
          </div>
        ) : null}
      </div>

      {post.versions_total > 1 ? (
        <div className="card tight">
          <div className="kicker">вариант {post.version} из {post.versions_total}</div>
          <div className="row wrap">
            {Array.from({ length: post.versions_total }, (_, index) => index + 1).map((number) => (
              <button
                key={number}
                type="button"
                className={`act small ${number === post.version ? '' : 'ghost'}`}
                onClick={async () => {
                  try { after(await api.switchVersion(post.id, number)); }
                  catch (problem) { onToast(problem.message, true); }
                }}
              >
                {number}
              </button>
            ))}
          </div>
          <div className="sub mt">Опубликовать можно любой вариант, не только последний.</div>
        </div>
      ) : null}

      {post.state === 'in_review' ? (
        <div className="card stack">
          {DECISIONS.map((item) => (
            <Action
              key={item.code}
              kind={item.kind || (item.warn ? 'ghost' : 'ghost')}
              price={item.costs ? money(item.costs * perImage) : null}
              onRun={act(item.code)}
            >
              <span>
                {item.label}
                <span className="price" style={{ display: 'block' }}>{item.note}</span>
              </span>
            </Action>
          ))}
          <button type="button" className="act danger" onClick={() => setAsking(true)}>
            Выбросить<span className="price">спросит, что именно</span>
          </button>
        </div>
      ) : null}

      {post.state === 'approved' ? (
        <div className="card stack">
          <div className="sub">Пост одобрен и ждёт публикации.</div>
          <Action kind="ghost" onRun={act('back')} done="Отменено">Отменить публикацию</Action>
        </div>
      ) : null}

      {post.state === 'published' ? (
        <div className="card">
          <div className="sub">Опубликован {when(post.published_at)}.</div>
          {post.external_id ? (
            <a className="act mt" href={`https://vk.com/wall${post.external_id}`} target="_blank" rel="noreferrer">
              Открыть в ВК
            </a>
          ) : null}
        </div>
      ) : null}

      {post.state === 'failed' ? (
        <div className="card alarm">
          <h2 style={{ color: 'inherit' }}>Пост сломался</h2>
          <div className="mono-block mt">{post.last_error}</div>
          <Action kind="ghost mt" onRun={act('fix')} done="Вернул в работу">Попробовать снова</Action>
        </div>
      ) : null}

      {asking ? (
        <Modal title="Что выбросить?" onClose={() => setAsking(false)}>
          <div className="stack">
            <Action
              kind="ghost"
              onRun={async () => { setAsking(false); await act('del')(); }}
            >
              <span>Только этот пост<span className="price" style={{ display: 'block' }}>
                тема вернётся в очередь, но в конец
              </span></span>
            </Action>
            <Action
              kind="danger"
              onRun={async () => { setAsking(false); await act('delt')(); }}
            >
              <span>Пост и тему<span className="price" style={{ display: 'block' }}>
                тему больше не возьмут
              </span></span>
            </Action>
            <button type="button" className="act ghost" onClick={() => setAsking(false)}>
              Не выбрасывать
            </button>
          </div>
        </Modal>
      ) : null}

      {editing ? (
        <EditModal
          post={post}
          field={editing}
          onClose={() => setEditing(null)}
          onSaved={(answer) => { setEditing(null); after(answer); }}
          onFail={(message) => onToast(message, true)}
        />
      ) : null}

      {promptFor ? (
        <PromptModal
          post={post}
          asset={promptFor}
          onClose={() => setPromptFor(null)}
          onSaved={(answer) => { setPromptFor(null); after(answer); }}
          onFail={(message) => onToast(message, true)}
        />
      ) : null}
    </>
  );
}

function EditModal({ post, field, onClose, onSaved, onFail }) {
  const [title, setTitle] = useState(post.title || '');
  const [body, setBody] = useState(post.body || '');
  const titleChanged = title.trim() !== (post.title || '').trim();

  return (
    <Modal title={field === 'title' ? 'Заголовок' : 'Текст поста'} onClose={onClose}>
      {field === 'title' ? (
        <label className="field">
          <span className="name">не длиннее 60 знаков — иначе не помещается на обложку</span>
          <input value={title} maxLength={60} onChange={(event) => setTitle(event.target.value)} />
        </label>
      ) : (
        <label className="field">
          <span className="name">картинки не меняются, сохранение ничего не стоит</span>
          <textarea value={body} rows={12} onChange={(event) => setBody(event.target.value)} />
        </label>
      )}

      {field === 'title' && titleChanged ? (
        <div className="card notice tight">
          Обложка соберётся заново — около минуты. Картинки не меняются.
        </div>
      ) : null}

      <div className="stack">
        <Action
          kind="main"
          onRun={async () => {
            try {
              onSaved(await api.editText(post.id, body, field === 'title' ? title : null));
            } catch (problem) { onFail(problem.message); throw problem; }
          }}
        >
          Сохранить
        </Action>
        <button type="button" className="act ghost" onClick={onClose}>Отмена</button>
      </div>
    </Modal>
  );
}

function PromptModal({ post, asset, onClose, onSaved, onFail }) {
  const [text, setText] = useState(asset.prompt || '');

  return (
    <Modal title="Промпт картинки" onClose={onClose}>
      <label className="field">
        <span className="name">по-английски: строка уходит в модель дословно</span>
        <textarea value={text} rows={7} onChange={(event) => setText(event.target.value)} />
      </label>
      <div className="stack">
        <Action
          kind="main"
          price={money(1.68)}
          onRun={async () => {
            try { onSaved(await api.redraw(post.id, asset.position, text)); }
            catch (problem) { onFail(problem.message); throw problem; }
          }}
        >
          Перерисовать по новому промпту
        </Action>
        <button type="button" className="act ghost" onClick={onClose}>Отмена</button>
      </div>
    </Modal>
  );
}
