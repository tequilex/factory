// Ключи и доступы. Самая частая поломка системы живёт здесь.

import React, { useState } from 'react';
import { api } from '../api.js';
import { Action, Failed, Loading, useData } from '../ui.jsx';

export default function Keys({ slug, onToast }) {
  const { loading, data, error, refresh } = useData(() => api.keys(slug), [slug]);
  const [code, setCode] = useState('');

  if (loading && !data) return <Loading what="Проверяю ключи…" />;
  if (error) return <Failed error={error} onRetry={refresh} />;

  const upload = data.keys[0];

  return (
    <>
      <h1>Ключи и доступы</h1>
      <p className="lead">Ключ загрузки живёт сутки — это самая частая поломка</p>

      <div className={`card ${upload.alive === false ? 'alarm' : ''}`}>
        <div className="spread">
          <h2 style={{ color: 'inherit' }}>{upload.title}</h2>
          <span className="sub" style={{ color: 'inherit' }}>
            <span className={`dot ${upload.alive ? 'ok' : 'bad'}`} />
            {upload.alive ? 'действителен' : 'не работает'}
          </span>
        </div>
        <div className="sub" style={{ color: 'inherit' }}>{upload.note}</div>
        {upload.tail ? <div className="faint num mt">{upload.env} · {upload.tail}</div> : null}

        {data.vk_code_url ? (
          <div className="mt">
            <div className="kicker">обновление в три шага</div>
            <a className="act" href={data.vk_code_url} target="_blank" rel="noreferrer">
              1. Открыть ВКонтакте и нажать «Разрешить»
            </a>
            <div className="sub mt">
              2. Откроется пустая белая страница. Скопируйте адрес из строки
              браузера целиком — вырезать ничего не надо.
            </div>
            <label className="field mt">
              <span className="name">3. Вставьте адрес сюда</span>
              <input
                value={code}
                placeholder="https://oauth.vk.ru/blank.html#code=…"
                onChange={(event) => setCode(event.target.value)}
              />
            </label>
            <Action
              kind="main"
              disabled={!code.trim()}
              onRun={async () => {
                try {
                  const answer = await api.sendVkCode(slug, code);
                  onToast(answer.what_next);
                  setCode('');
                  refresh();
                } catch (problem) { onToast(problem.message, true); throw problem; }
              }}
            >
              Обменять код на ключ
            </Action>
            <div className="faint mt">
              Адрес содержит одноразовый код, а не сам ключ: ключ система
              выписывает себе сама, со своего адреса. Поэтому обновлять можно
              откуда угодно — хоть из отпуска.
            </div>
          </div>
        ) : (
          <div className="card notice tight mt">
            В настройках группы не заданы <span className="num">vk.app_id</span> и
            <span className="num"> vk.app_secret_env</span> — обменять код не на что.
          </div>
        )}
      </div>

      {data.keys.slice(1).map((key) => (
        <div className="card" key={key.env}>
          <div className="spread">
            <h2>{key.title}</h2>
            <span className="sub">
              <span className={`dot ${key.present ? 'ok' : 'bad'}`} />
              {key.present ? 'задан' : 'не задан'}
            </span>
          </div>
          <div className="sub">{key.purpose}</div>
          {key.tail ? <div className="faint num mt">{key.env} · {key.tail}</div> : null}
          {key.note ? <div className="faint mt">{key.note}</div> : null}
        </div>
      ))}

      <div className="card">
        <h2>Связь с группой</h2>
        <div className="sub">Проверка идёт настоящим вызовом ВКонтакте.</div>
        <CheckAccess slug={slug} onToast={onToast} />
      </div>

      <div className="faint">
        Ключи целиком не показываются нигде — только последние символы. Панель
        открыта в браузере, а браузеры хранят историю и кэш.
      </div>
    </>
  );
}

function CheckAccess({ slug, onToast }) {
  const [result, setResult] = useState(null);

  return (
    <>
      <Action
        kind="ghost mt"
        done="Проверено"
        onRun={async () => {
          try {
            const answer = await api.checkAccess(slug);
            setResult(answer);
            if (!answer.ok) onToast(answer.detail, true);
          } catch (problem) { onToast(problem.message, true); throw problem; }
        }}
      >
        Проверить доступ
      </Action>
      {result ? (
        <div className={`card tight mt ${result.ok ? '' : 'alarm'}`}>
          <span className={`dot ${result.ok ? 'ok' : 'bad'}`} />
          {result.detail}
        </div>
      ) : null}
    </>
  );
}
