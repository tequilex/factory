// Просмотр поста — главный экран. На телефоне колонкой, на компьютере в две.

import React, { useEffect, useRef, useState } from 'react';
import { api, money, when } from '../api.js';
import { Btn, Choice, Failed, Loading, Sheet, useData } from '../ui.jsx';

const FACTCHECK = {
  fixed: 'Фактчек исправил текст.',
  uncertain: 'Фактчек не уверен.',
};

const PER_IMAGE = 1.68;

export default function Post({ id, onBack, onToast }) {
  const { loading, data, error, refresh } = useData(() => api.post(id), [id]);
  const [shown, setShown] = useState(0);
  const [stamp, setStamp] = useState(() => Date.now());
  const [ask, setAsk] = useState(false);
  const [editPrompt, setEditPrompt] = useState(null);

  useEffect(() => { setShown(0); setStamp(Date.now()); }, [id]);

  if (loading && !data) return <Loading what="Открываю пост…" />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const post = data;
  const ready = post.assets.filter((asset) => asset.ready);
  const current = ready[Math.min(shown, Math.max(ready.length - 1, 0))];
  const images = money(PER_IMAGE * ready.length || PER_IMAGE * 4);

  const after = (answer) => {
    onToast(answer.what_next);
    setStamp(Date.now());
    refresh();
  };

  const decide = (code) => async () => {
    try {
      after(await api.decide(post.id, code, post.version));
    } catch (problem) {
      onToast(problem.message, true);
      refresh();
      throw problem;
    }
  };

  const replace = async (position, file) => {
    try {
      after(await api.uploadImage(post.id, position, file));
    } catch (problem) { onToast(problem.message, true); }
  };

  return (
    <>
      <div className="head">
        <div className="grow">
          <div className="row wrap" style={{ marginBottom: 6 }}>
            <button type="button" className="btn plain none" onClick={onBack} style={{ padding: '0 8px' }}>‹</button>
            <span className="chip"><span className="dot s ok" />{post.state_label}</span>
            <span className="faint">
              пост {post.id} · {post.project} · создан {when(post.created_at)} · {money(post.cost)}
            </span>
          </div>
          <h1>{post.title || 'Без заголовка'}</h1>
        </div>

        {post.versions_total > 1 ? (
          <div className="row">
            <span className="muted">Вариант</span>
            <div className="seg">
              {Array.from({ length: post.versions_total }, (_, index) => index + 1).map((number) => (
                <button
                  key={number}
                  type="button"
                  data-on={number === post.version ? '1' : '0'}
                  onClick={async () => {
                    try { after(await api.switchVersion(post.id, number)); }
                    catch (problem) { onToast(problem.message, true); }
                  }}
                >
                  {number}
                </button>
              ))}
            </div>
            <span className="faint">опубликовать можно любой</span>
          </div>
        ) : null}
      </div>

      <div className="two">
        <div className="col gap-14">
          {ready.length ? (
            <>
              <div className="four wide-only">
                {ready.map((asset) => (
                  <Shot
                    key={asset.position}
                    post={post}
                    asset={asset}
                    stamp={stamp}
                    onReplace={replace}
                    onPrompt={() => setEditPrompt(asset)}
                  />
                ))}
              </div>

              <div className="narrow-only col gap-10">
                <Shot post={post} asset={current} stamp={stamp} onReplace={replace} big />
                <div className="strip">
                  {ready.map((asset, index) => (
                    <button
                      key={asset.position}
                      type="button"
                      data-on={index === shown ? '1' : '0'}
                      onClick={() => setShown(index)}
                    >
                      <img src={api.imageUrl(post.id, asset.position, stamp)} alt="" loading="lazy" />
                      {asset.replaced_by_owner ? <span className="mine">ваша</span> : null}
                    </button>
                  ))}
                </div>
                <div className="faint">Тап по миниатюре листает. Заменённая вами картинка помечена.</div>
              </div>

              <label className="drop">
                Перетащите файл на картинку, чтобы заменить своей — или <b>выберите файл</b>.
                Заменённая помечается.
                <input
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = '';
                    if (file) replace(current.position, file);
                  }}
                />
              </label>

              <div className="card soft col gap-10">
                <div className="kicker">
                  Промпт картинки {current.kind === 'cover' ? '1 (обложка)' : current.position + 1}
                </div>
                <div className="pre">{current.prompt || '—'}</div>
                <div className="row wrap">
                  <Btn
                    kind="line"
                    done="Отправлено"
                    onRun={async () => {
                      try { after(await api.redraw(post.id, current.position, null)); }
                      catch (problem) { onToast(problem.message, true); throw problem; }
                    }}
                  >
                    Перерисовать только эту · {money(PER_IMAGE)}
                  </Btn>
                  <button type="button" className="btn" onClick={() => setEditPrompt(current)}>
                    Изменить промпт
                  </button>
                </div>
                <div className="faint">
                  Первая часть промпта — приметы персонажа из настроек группы.
                </div>
              </div>
            </>
          ) : (
            <div className="card muted">Картинок пока нет — они ещё рисуются.</div>
          )}
        </div>

        <div className="col gap-14">
          <div className="field">
            <div className="spread">
              <span className="kicker">Заголовок</span>
              <span className="faint">{(post.title || '').length} из 60 знаков</span>
            </div>
            <Title post={post} onSaved={after} onFail={(message) => onToast(message, true)} />
          </div>

          {post.waiting_reason && post.state !== 'in_review' ? (
        <div className="banner warn">
          <div className="row top" style={{ gap: 10 }}>
            <span className="dot warn" style={{ marginTop: 6 }} />
            <span style={{ font: '400 13px/1.5 var(--sans)', color: 'var(--warn-text)' }}>
              <b style={{ fontWeight: 600 }}>Пост стоит.</b> {post.waiting_reason}
            </span>
          </div>
        </div>
      ) : null}

      {post.factcheck_verdict && FACTCHECK[post.factcheck_verdict] ? (
            <div className="banner warn">
              <div className="row top" style={{ gap: 10 }}>
                <span className="dot warn" style={{ marginTop: 6 }} />
                <span style={{ font: '400 13px/1.5 var(--sans)', color: 'var(--warn-text)' }}>
                  <b style={{ fontWeight: 600 }}>{FACTCHECK[post.factcheck_verdict]}</b>{' '}
                  {post.factcheck_notes}
                </span>
              </div>
            </div>
          ) : null}

          <Body post={post} onSaved={after} onFail={(message) => onToast(message, true)} />

          {post.question ? (
            <div className="card soft">
              <div className="kicker" style={{ marginBottom: 6 }}>Вопрос-хук</div>
              <div>{post.question}</div>
            </div>
          ) : null}

          {post.state === 'in_review' ? (
            <div className="col gap-10" style={{ borderTop: '1px solid var(--line-4)', paddingTop: 16 }}>
              <Choice
                kind="main"
                what="Опубликовать"
                then="Уйдёт в группу ближайшей публикацией"
                busy="Одобрен, ждёт слота"
                done="Одобрен, ждёт слота"
                onRun={decide('ok')}
              />
              <div className="pair">
                <Choice what="Другие сцены" then={`текст остаётся · ${images}`} onRun={decide('scn')} />
                <Choice what="Картинки заново" then={`те же сцены · ${images}`} onRun={decide('img')} />
                <Choice kind="warn" what="Текст заново" then={`и новые картинки · ${images}`} onRun={decide('txt')} />
                <button type="button" className="choice bad" onClick={() => setAsk(true)}>
                  <span className="what">Выбросить</span>
                  <span className="then">спросит, что именно</span>
                </button>
              </div>
              <div className="faint">
                Переделка стоит денег и занимает несколько минут: новый вариант придёт
                отдельно, этот останется.
              </div>
            </div>
          ) : null}

          {post.state === 'approved' ? (
            <div className="card soft col gap-8">
              <span className="row" style={{ font: '500 16px/1.2 var(--sans)', color: 'var(--accent-2)' }}>
                <span className="dot ok" />Одобрен, ждёт слота
              </span>
              <span className="muted">До публикации решение ещё можно отменить.</span>
              <Choice kind="bad" what="Отменить публикацию" then="пост вернётся на просмотр" onRun={decide('back')} />
            </div>
          ) : null}

          {post.state === 'published' ? (
            <div className="card soft col gap-8">
              <span className="row" style={{ font: '500 16px/1.2 var(--sans)' }}>
                <span className="dot soft" />Опубликован {when(post.published_at)}
              </span>
              {post.external_id ? (
                <a className="btn line" href={`https://vk.com/wall${post.external_id}`} target="_blank" rel="noreferrer">
                  Открыть в ВК
                </a>
              ) : null}
            </div>
          ) : null}

          {post.state === 'failed' ? (
            <div className="banner bad">
              <div className="grow">
                <div className="head-line"><span className="dot bad" />Пост сломался</div>
                <div className="pre" style={{ marginTop: 8 }}>{post.last_error}</div>
              </div>
              <Btn kind="line none" done="Вернул в работу" onRun={decide('fix')}>Попробовать снова</Btn>
            </div>
          ) : null}

          <div className="stats-foot">
            <span>Группа<br /><b>{post.project}</b></span>
            <span>Состояние<br /><b>{post.state_label}</b></span>
            <span>Стоимость поста<br /><b className="mono">{money(post.cost)}</b></span>
            <span>Создан<br /><b>{when(post.created_at)}</b></span>
          </div>
        </div>
      </div>

      {ask ? (
        <Sheet title="Что выбросить?" onClose={() => setAsk(false)}>
          <Choice
            what="Только этот пост"
            then="Тема вернётся в очередь, но в конец. Дойдёт — попробуем ещё раз."
            onRun={async () => { setAsk(false); await decide('del')(); }}
          />
          <Choice
            kind="bad"
            what="Пост и тему"
            then="Тема закроется, больше по ней не пишем. Отменить нельзя."
            onRun={async () => { setAsk(false); await decide('delt')(); }}
          />
          <button type="button" className="btn plain" onClick={() => setAsk(false)}>Не выбрасывать</button>
        </Sheet>
      ) : null}

      {editPrompt ? (
        <PromptSheet
          post={post}
          asset={editPrompt}
          onClose={() => setEditPrompt(null)}
          onSaved={(answer) => { setEditPrompt(null); after(answer); }}
          onFail={(message) => onToast(message, true)}
        />
      ) : null}
    </>
  );
}

function Shot({ post, asset, stamp, onReplace, onPrompt, big }) {
  const input = useRef(null);
  const [over, setOver] = useState(false);

  return (
    <div
      className={`shot ${asset.replaced_by_owner ? 'own' : ''}`}
      style={over ? { borderColor: 'var(--accent)' } : null}
      onDragOver={(event) => { event.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        const file = event.dataTransfer.files?.[0];
        if (file) onReplace(asset.position, file);
      }}
      onClick={onPrompt}
    >
      <img src={api.imageUrl(post.id, asset.position, stamp)} alt="" loading="lazy" />
      <span className={`tag ${asset.replaced_by_owner ? 'mine' : ''}`}>
        {asset.replaced_by_owner
          ? `ваша картинка · ${asset.position + 1}`
          : asset.kind === 'cover' ? 'обложка · заголовок системы' : `сцена ${asset.position + 1}`}
      </span>
      {big ? <span className="count">{asset.position + 1} / 4</span> : null}
      <input ref={input} type="file" accept="image/*" hidden />
    </div>
  );
}

function Title({ post, onSaved, onFail }) {
  const [value, setValue] = useState(post.title || '');
  useEffect(() => setValue(post.title || ''), [post.title, post.version]);
  const changed = value.trim() !== (post.title || '').trim();

  return (
    <>
      <input value={value} maxLength={60} onChange={(event) => setValue(event.target.value)} />
      <div className="row wrap">
        <Btn
          kind={changed ? 'main' : ''}
          disabled={!changed || !value.trim()}
          done="Сохранено"
          onRun={async () => {
            try { onSaved(await api.editText(post.id, post.body || '', value)); }
            catch (problem) { onFail(problem.message); throw problem; }
          }}
        >
          Сохранить и перерисовать обложку
        </Btn>
        <span style={{ font: '400 12px/1.4 var(--sans)', color: 'var(--warn-text)' }}>
          Обложка соберётся заново, около минуты. Картинки не меняются, денег не стоит.
        </span>
      </div>
    </>
  );
}

function Body({ post, onSaved, onFail }) {
  const [value, setValue] = useState(post.body || '');
  useEffect(() => setValue(post.body || ''), [post.body, post.version]);
  const changed = value !== (post.body || '');

  return (
    <div className="field">
      <div className="spread">
        <span className="kicker">Текст поста</span>
        <span className="faint">{value.length} знаков · правится на месте</span>
      </div>
      <textarea rows={12} value={value} onChange={(event) => setValue(event.target.value)} />
      <div className="row wrap">
        <Btn
          disabled={!changed || !value.trim()}
          done="Сохранено"
          onRun={async () => {
            try { onSaved(await api.editText(post.id, value, null)); }
            catch (problem) { onFail(problem.message); throw problem; }
          }}
        >
          Сохранить текст
        </Btn>
        <span className="faint">Сохранение текста не пересобирает картинки и ничего не стоит.</span>
      </div>
    </div>
  );
}

function PromptSheet({ post, asset, onClose, onSaved, onFail }) {
  const [text, setText] = useState(asset.prompt || '');

  return (
    <Sheet title={`Промпт картинки ${asset.position + 1}`} onClose={onClose}>
      <div className="faint">По-английски: строка уходит в модель дословно.</div>
      <textarea rows={8} value={text} onChange={(event) => setText(event.target.value)} />
      <Choice
        kind="main"
        what={`Перерисовать по новому промпту · ${money(PER_IMAGE)}`}
        then="остальные картинки не тронуты"
        onRun={async () => {
          try { onSaved(await api.redraw(post.id, asset.position, text)); }
          catch (problem) { onFail(problem.message); throw problem; }
        }}
      />
      <button type="button" className="btn plain" onClick={onClose}>Отмена</button>
    </Sheet>
  );
}
