"""Ни одного захардкоженного пути: всё из окружения, со значениями по умолчанию."""

import os
from pathlib import Path

import pytest

from factory.core import paths
from factory.core.errors import FactoryError

ENV_VARS = [
    "FACTORY_DATA_DIR",
    "FACTORY_TMP_DIR",
    "FACTORY_PROJECTS_DIR",
    "FACTORY_MIGRATIONS_DIR",
    "FACTORY_MAX_PARALLEL_IMAGES",
    "FACTORY_MAX_STEPS_PER_TICK",
    "FACTORY_TICK_INTERVAL_SEC",
    "FACTORY_LOCK_TTL_SEC",
    "FACTORY_IGNORE_SCHEDULE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Значения по умолчанию проверяются только на пустом окружении."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_the_spec():
    assert paths.data_dir() == Path("/data")
    assert paths.tmp_dir() == Path("/tmp/factory")
    assert paths.projects_dir() == Path("/app/projects")
    assert paths.max_parallel_images() == 1
    assert paths.max_steps_per_tick() == 3
    assert paths.tick_interval_sec() == 600
    assert paths.lock_ttl_sec() == 1800
    assert paths.ignore_schedule() is False


def test_derived_paths_hang_off_the_data_dir(monkeypatch):
    monkeypatch.setenv("FACTORY_DATA_DIR", "/srv/factory")
    assert paths.db_path() == Path("/srv/factory/factory.db")
    assert paths.backups_dir() == Path("/srv/factory/backups")
    assert paths.env_file() == Path("/srv/factory/.env")


def test_env_overrides_every_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FACTORY_TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("FACTORY_PROJECTS_DIR", str(tmp_path / "projects"))

    assert paths.data_dir() == tmp_path / "data"
    assert paths.tmp_dir() == tmp_path / "tmp"
    assert paths.projects_dir() == tmp_path / "projects"


def test_env_overrides_every_number(monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_PARALLEL_IMAGES", "4")
    monkeypatch.setenv("FACTORY_MAX_STEPS_PER_TICK", "1")
    monkeypatch.setenv("FACTORY_TICK_INTERVAL_SEC", "5")
    monkeypatch.setenv("FACTORY_LOCK_TTL_SEC", "60")

    assert paths.max_parallel_images() == 4
    assert paths.max_steps_per_tick() == 1
    assert paths.tick_interval_sec() == 5
    assert paths.lock_ttl_sec() == 60


def test_tilde_is_expanded(monkeypatch):
    """README советует ~/factory-data — подсказка должна работать буквально."""
    monkeypatch.setenv("FACTORY_DATA_DIR", "~/factory-data")
    assert paths.data_dir() == Path.home() / "factory-data"


def test_post_tmp_dir_is_per_post(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_TMP_DIR", str(tmp_path))
    assert paths.post_tmp_dir(42) == tmp_path / "42"


def test_migrations_dir_is_found_next_to_the_package():
    assert paths.migrations_dir().name == "migrations"


def test_migrations_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_MIGRATIONS_DIR", str(tmp_path))
    assert paths.migrations_dir() == tmp_path


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_ignore_schedule_accepts_common_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", raw)
    assert paths.ignore_schedule() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_ignore_schedule_accepts_common_falsy_spellings(monkeypatch, raw):
    monkeypatch.setenv("FACTORY_IGNORE_SCHEDULE", raw)
    assert paths.ignore_schedule() is False


def test_non_numeric_value_explains_itself(monkeypatch):
    monkeypatch.setenv("FACTORY_TICK_INTERVAL_SEC", "abc")
    with pytest.raises(FactoryError) as excinfo:
        paths.tick_interval_sec()

    message = str(excinfo.value)
    assert "FACTORY_TICK_INTERVAL_SEC" in message
    assert "abc" in message
    assert "Что делать:" in message


def test_zero_and_negative_numbers_are_rejected(monkeypatch):
    """Тик с интервалом 0 — бесконечный цикл без пауз, съедающий CPU на Pi."""
    monkeypatch.setenv("FACTORY_TICK_INTERVAL_SEC", "0")
    with pytest.raises(FactoryError):
        paths.tick_interval_sec()


class TestEnsureDataDir:
    def test_creates_the_directory_and_returns_it(self, monkeypatch, tmp_path):
        target = tmp_path / "data" / "nested"
        monkeypatch.setenv("FACTORY_DATA_DIR", str(target))

        assert paths.ensure_data_dir() == target
        assert target.is_dir()

    def test_is_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FACTORY_DATA_DIR", str(tmp_path / "data"))
        paths.ensure_data_dir()
        paths.ensure_data_dir()

    def test_parallel_starts_do_not_collide(self, monkeypatch, tmp_path):
        """В docker compose три сервиса стартуют одновременно на одном томе.

        С общим именем пробного файла они удаляли его друг у друга, и запуск
        падал с «No such file or directory» — причём не всегда, что хуже всего.
        """
        import threading

        monkeypatch.setenv("FACTORY_DATA_DIR", str(tmp_path / "shared"))
        errors: list[BaseException] = []
        barrier = threading.Barrier(12)

        def start() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    paths.ensure_data_dir()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"параллельный запуск упал: {errors}"

    def test_probe_file_does_not_stay_behind(self, monkeypatch, tmp_path):
        target = tmp_path / "data"
        monkeypatch.setenv("FACTORY_DATA_DIR", str(target))

        paths.ensure_data_dir()

        assert not list(target.glob(".write-probe*")), "пробный файл остался в каталоге данных"

    def test_unwritable_parent_gives_advice_not_permission_error(self, monkeypatch, tmp_path):
        """Умолчание /data рассчитано на контейнер; на macOS без sudo оно не создаётся."""
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        monkeypatch.setenv("FACTORY_DATA_DIR", str(readonly / "data"))

        try:
            with pytest.raises(FactoryError) as excinfo:
                paths.ensure_data_dir()
        finally:
            readonly.chmod(0o700)

        message = str(excinfo.value)
        assert "FACTORY_DATA_DIR" in message
        assert "export FACTORY_DATA_DIR=" in message
        assert str(readonly / "data") in message

    @pytest.mark.skipif(os.geteuid() == 0, reason="под root каталог /data создастся")
    def test_default_data_dir_on_macos_gives_advice(self, monkeypatch):
        monkeypatch.setenv("FACTORY_DATA_DIR", "/data")
        with pytest.raises(FactoryError) as excinfo:
            paths.ensure_data_dir()
        assert "export FACTORY_DATA_DIR=" in str(excinfo.value)

    def test_existing_but_unwritable_directory_is_caught(self, monkeypatch, tmp_path):
        """Каталог есть, но прав на запись нет — база не откроется, надо сказать заранее."""
        target = tmp_path / "locked-data"
        target.mkdir()
        target.chmod(0o500)
        monkeypatch.setenv("FACTORY_DATA_DIR", str(target))

        try:
            with pytest.raises(FactoryError) as excinfo:
                paths.ensure_data_dir()
        finally:
            target.chmod(0o700)

        assert "export FACTORY_DATA_DIR=" in str(excinfo.value)
