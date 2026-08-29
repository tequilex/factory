"""HTTP-слой панели.

Панель — четвёртый процесс рядом с воркером, ботом и прокси. Она **ничего не
выполняет**: одобрение не публикует, правка не пересобирает обложку. Всё это
делает воркер на следующем проходе, а панель только ставит отметки в базе. Любой
экран, обещающий мгновенный результат, здесь врёт — это записано в брифе первым
пунктом и в коде поддерживается тем, что выполнять отсюда попросту нечем.

Наружу порты не открываются: доступ идёт через Tailscale, то есть до панели
дотягиваются только устройства владельца. Поэтому нет ни TLS, ни блокировок
после неудачных попыток — от чужих защищает сеть, а пароль закрывает потерянный
телефон.
"""

from __future__ import annotations

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from factory.core.errors import FactoryError
from factory.core.logging import get_logger
from factory.panel import auth

log = get_logger(__name__)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)
    trusted: bool = False


def require_session(factory_panel: str | None = Cookie(default=None)) -> None:
    """Пускать только с годной кукой.

    Зависимость вешается на всё, кроме входа. Забыть её на одном обработчике —
    открыть данные наружу, поэтому проверка одна на всех и тестом закреплена.
    """
    if not auth.check_cookie(factory_panel):
        raise HTTPException(status_code=401, detail="Нужен вход.")


def create_app() -> FastAPI:
    # Документация отключена целиком, вместе с openapi.json: панель не публичный
    # API, а список ручек наружу — лишняя подсказка тому, кто всё-таки окажется
    # в сети. Отключить только страницы недостаточно: схема отдаётся отдельным
    # адресом и остаётся доступной без входа.
    app = FastAPI(
        title="Панель контент-фабрики",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post("/api/login")
    def login(body: LoginRequest, response: Response) -> dict:
        try:
            ok = auth.check_password(body.password)
        except FactoryError as exc:
            # Пароль не задан вовсе: это настройка, а не отказ в доступе, и
            # владелец должен увидеть, что именно сделать.
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if not ok:
            # Пароль в лог не попадает даже случайно: пишем только сам факт.
            log.warning("неудачная попытка входа в панель")
            raise HTTPException(status_code=401, detail="Пароль не подошёл.")

        response.set_cookie(
            auth.COOKIE_NAME,
            auth.issue_cookie(trusted=body.trusted),
            httponly=True,
            # Lax, а не Strict: по ссылке из уведомления в Telegram переход
            # считается межсайтовым, и при Strict владелец каждый раз попадал бы
            # на экран входа. От межсайтовых POST-запросов Lax при этом защищает.
            samesite="lax",
            # Не secure: за Tailscale соединение идёт по http, и кука с этим
            # флагом просто не отправлялась бы. Сеть закрыта на своём уровне.
            secure=False,
            max_age=auth.DEFAULT_DAYS * 86400 if body.trusted else auth.SHORT_HOURS * 3600,
        )
        log.info("вход в панель выполнен", extra={"trusted": body.trusted})
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(auth.COOKIE_NAME)
        return {"ok": True}

    @app.get("/api/session")
    def session(_: None = Depends(require_session)) -> dict:
        """Годен ли вход. Нужен фронту, чтобы решить, показывать экран входа."""
        return {"ok": True}

    return app
