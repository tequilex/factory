"""Сборка обложки.

Главное требование спеки: длинный кириллический заголовок обязан поместиться в
плашку. Проверяется по фактическим границам отрисованного текста, а не по тому,
что функция не упала.
"""

import io
import json

import pytest
from PIL import Image

from factory.compose import cover
from factory.core.errors import FactoryError

# Заголовок ровно предельной длины из спеки.
LONG_TITLE = "Три года ездила и не знала, что так вообще можно было"
SHORT_TITLE = "Коротко"


@pytest.fixture
def template(demo_project):
    return demo_project / "templates" / "red_frame.json"


@pytest.fixture
def background():
    """Фон, поверх которого собирается обложка, — как из генератора картинок."""
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 1350), (40, 60, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


def build(template, background, title=LONG_TITLE) -> Image.Image:
    return Image.open(io.BytesIO(cover.render(background, title, template)))


def roomy(template) -> dict:
    """Шаблон с заведомо большим потолком кегля.

    Боевой шаблон держит `size_max` небольшим — иначе заголовок закрывает
    картинку. Но тогда и длинный заголовок влезает в максимум, и подбору кегля
    нечего подбирать. Проверять сам подбор надо там, где он работает, иначе
    тест зелен потому, что ничего не проверяет.
    """
    spec = json.loads(template.read_text(encoding="utf-8"))
    spec["title"]["size_max"] = 130
    return spec


class TestOutput:
    def test_result_is_a_png_of_the_right_size(self, template, background):
        image = build(template, background)

        assert image.format == "PNG"
        assert image.size == (1080, 1350)

    def test_same_input_gives_identical_bytes(self, template, background):
        """Иначе шаг compose не идемпотентен и повторный тик перерисовывает."""
        first = cover.render(background, LONG_TITLE, template)
        second = cover.render(background, LONG_TITLE, template)

        assert first == second

    def test_background_shows_through_outside_the_plate(self, template, background):
        """Картинка — фон, а не подложка под сплошную заливку."""
        image = build(template, background)

        # Точка заведомо ниже плашки и внутри рамки.
        assert image.getpixel((540, 1200))[:3] == (40, 60, 90)


class TestFrame:
    def test_frame_is_drawn_on_every_side(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        colour = cover.parse_colour(spec["frame"]["color"])
        image = build(template, background)
        width, height = image.size

        for point in [(2, height // 2), (width - 3, height // 2), (width // 2, 2), (width // 2, height - 3)]:
            assert image.getpixel(point)[:3] == colour, f"нет рамки в точке {point}"

    def test_frame_thickness_matches_the_template(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        thickness = spec["frame"]["width_px"]
        colour = cover.parse_colour(spec["frame"]["color"])
        image = build(template, background)

        assert image.getpixel((thickness - 2, 700))[:3] == colour
        assert image.getpixel((thickness + 4, 700))[:3] != colour


def plate_bottom(image: Image.Image, spec: dict) -> int:
    """Нижняя граница плашки на готовой обложке, по цвету пикселей.

    Меряется по картинке, а не по числу из шаблона: проверяется именно то, что
    закрывает фотографию, а не то, что было задумано.
    """
    colour = cover.parse_colour(spec["plate"]["color"])
    x = spec["plate"]["x"] + 4
    last = spec["plate"]["y"]
    for y in range(spec["plate"]["y"], spec["canvas"]["height"]):
        if image.getpixel((x, y))[:3] == colour:
            last = y
    return last


class TestPlate:
    def test_plate_is_filled_with_its_colour(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        image = build(template, background)

        corner = (plate["x"] + 4, plate["y"] + 4)
        assert image.getpixel(corner)[:3] == cover.parse_colour(plate["color"])

    def test_plate_shrinks_to_a_short_title(self, template, background):
        """Плашка по тексту, а не по худшему случаю.

        Иначе заголовок в два слова закрывает белым ту же четверть картинки, что
        и заголовок в две строки, — просто так.
        """
        spec = json.loads(template.read_text(encoding="utf-8"))

        short = plate_bottom(build(template, background, SHORT_TITLE), spec)
        long = plate_bottom(build(template, background, LONG_TITLE), spec)

        assert short < long

    def test_plate_never_grows_past_the_template_height(self, template, background):
        """Высота из шаблона остаётся потолком.

        Проверяется на шаблоне, где кегль ужаться не может (`size_min` равен
        `size_max`): иначе подбор всегда впишет текст в плашку сам, потолок
        никогда не сработает, и тест будет зелёным, ничего не проверяя.
        """
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["title"]["size_min"] = spec["title"]["size_max"]
        rigid = template.parent / "rigid.json"
        rigid.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        image = build(rigid, background, LONG_TITLE * 2)

        assert plate_bottom(image, spec) <= spec["plate"]["y"] + spec["plate"]["height"]

    def test_padding_is_respected_when_choosing_the_size(self, template):
        """Отступы плашки — часть доступного места, а не украшение.

        Без них подбор считает, что текст можно вести впритык к белому краю, и
        заголовок в бою упирается в границу плашки.
        """
        spec = roomy(template)
        tight = json.loads(json.dumps(spec))
        tight["plate"]["padding"] = 140

        assert cover.fit_font_size(LONG_TITLE, tight) < cover.fit_font_size(LONG_TITLE, spec)

    def test_without_fit_to_text_the_plate_keeps_its_height(self, template, background):
        """Старое поведение остаётся доступным: это настройка, а не смена правил."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["plate"]["fit_to_text"] = False
        fixed = template.parent / "fixed.json"
        fixed.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        short = plate_bottom(build(fixed, background, SHORT_TITLE), spec)
        long = plate_bottom(build(fixed, background, LONG_TITLE), spec)

        assert short == long

    def test_title_stays_centred_in_the_shrunken_plate(self, template, background):
        """Текст обязан ехать вместе с плашкой, иначе он вылезет за её край."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        image = build(template, background, SHORT_TITLE)

        box = cover.text_bounds(image, cover.title_colours(spec))
        bottom_of_plate = plate_bottom(image, spec)

        assert box is not None
        assert box[3] <= bottom_of_plate


class TestTitle:
    def test_long_cyrillic_title_fits_inside_the_plate(self, template, background):
        """Требование спеки, ради которого этот модуль и существует."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        padding = plate["padding"]
        image = build(template, background, LONG_TITLE)

        box = cover.text_bounds(image, cover.title_colours(spec))

        assert box is not None, "текст не отрисован"
        left, top, right, bottom = box
        assert left >= plate["x"] + padding - 1
        assert top >= plate["y"] + padding - 1
        assert right <= plate["x"] + plate["width"] - padding + 1
        assert bottom <= plate["y"] + plate["height"] - padding + 1

    def test_short_title_is_set_larger_than_a_long_one(self, template, background):
        """Автоподбор кегля: короткий заголовок должен занимать плашку крупнее."""
        spec = roomy(template)

        short = cover.fit_font_size(SHORT_TITLE, spec)
        long = cover.fit_font_size(LONG_TITLE, spec)

        assert short > long

    def test_chosen_size_is_the_largest_that_fits(self, template, background):
        """Подбор обязан брать самый крупный подходящий кегль, а не любой подходящий.

        Без этой проверки подмена «пробовать только максимум» проходит незаметно:
        функция сваливается на минимальный кегль, тесты остаются зелёными, а
        заголовки в бою рисуются мелкими.
        """
        spec = roomy(template)
        plate = spec["plate"]
        padding = plate["padding"]
        available_width = plate["width"] - 2 * padding
        available_height = plate["height"] - 2 * padding
        spacing = spec["title"].get("line_spacing", 1.1)

        chosen = cover.fit_font_size(LONG_TITLE, spec)
        assert chosen < spec["title"]["size_max"], "подобрать нечего: заголовок влез в максимум"

        # Мерить надо ровно тот текст, который рисуется: заглавные шире строчных,
        # и на исходном регистре проверка была бы про другой заголовок.
        prepared = cover.prepare(LONG_TITLE, spec)
        bigger = cover.load_font(chosen + 1, spec["title"]["font"])
        width, height = cover._block_size(
            cover.wrap(prepared, bigger, available_width), bigger, spacing
        )

        assert width > available_width or height > available_height, (
            f"кегль {chosen + 1} тоже помещается — выбран не самый крупный"
        )

    def test_font_size_stays_within_the_configured_range(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))

        for title in [SHORT_TITLE, LONG_TITLE, "а"]:
            size = cover.fit_font_size(title, spec)
            assert spec["title"]["size_min"] <= size <= spec["title"]["size_max"]

    def test_words_are_not_broken_in_the_middle(self, template):
        spec = json.loads(template.read_text(encoding="utf-8"))
        font = cover.load_font(cover.fit_font_size(LONG_TITLE, spec), spec["title"]["font"])
        available = spec["plate"]["width"] - 2 * spec["plate"]["padding"]
        prepared = cover.prepare(LONG_TITLE, spec)

        lines = cover.wrap(prepared, font, available)

        assert " ".join(lines) == prepared, "слова переставлены или разорваны"
        assert len(lines) > 1, "длинный заголовок должен переноситься"

    def test_a_single_unbreakable_word_does_not_crash(self, template, background):
        """Слово длиннее строки: кегль опустится до минимума, но сборка пройдёт."""
        monster = "Достопримечательность" * 3

        image = build(template, background, monster)

        assert image.size == (1080, 1350)

    def test_title_is_actually_drawn(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        colours = cover.title_colours(spec)

        assert cover.text_bounds(build(template, background, LONG_TITLE), colours)
        assert cover.text_bounds(build(template, background, ""), colours) is None


class TestLook:
    """Свойства оформления, заданные шаблоном.

    Всё это задаётся в JSON и меняется без правки кода — тесты стерегут, что
    шаблон действительно управляет отрисовкой, а не игнорируется.
    """

    def test_uppercase_is_applied(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        assert spec["title"]["uppercase"] is True

        assert cover.prepare("Три года", spec) == "ТРИ ГОДА"

    def test_uppercase_can_be_switched_off(self, template):
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["title"]["uppercase"] = False

        assert cover.prepare("Три года", spec) == "Три года"

    def test_first_line_uses_the_accent_colour(self, template, background):
        """В референсе первая строка выделена цветом — на этом держится акцент."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        image = build(template, background, LONG_TITLE)

        accent = cover.text_bounds(image, cover.parse_colour(spec["title"]["accent_color"]))
        base = cover.text_bounds(image, cover.parse_colour(spec["title"]["color"]))

        assert accent is not None, "акцентная строка не нарисована"
        assert base is not None, "остальные строки не нарисованы"
        assert accent[1] < base[1], "акцентная строка должна быть первой сверху"
        assert accent[3] <= base[1] + 5, "строки наложились друг на друга"

    def test_without_accent_colour_all_lines_are_the_same(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["title"].pop("accent_color")
        template.write_text(json.dumps(spec), encoding="utf-8")
        image = build(template, background, LONG_TITLE)

        assert cover.text_bounds(image, cover.parse_colour("#8E1B18")) is None

    def test_lines_are_centred(self, template, background):
        """Центрирование: поля слева и справа должны совпадать."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        image = build(template, background, "Короткий")

        box = cover.text_bounds(image, cover.title_colours(spec))
        left_gap = box[0] - plate["x"]
        right_gap = plate["x"] + plate["width"] - box[2]

        assert abs(left_gap - right_gap) <= 6, f"поля разные: {left_gap} и {right_gap}"

    def test_left_alignment_still_works(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["title"]["align"] = "left"
        template.write_text(json.dumps(spec), encoding="utf-8")
        image = build(template, background, "Короткий")

        box = cover.text_bounds(image, cover.title_colours(spec))

        assert box[0] - spec["plate"]["x"] <= spec["plate"]["padding"] + 6

    def test_font_comes_from_the_template(self, template):
        spec = json.loads(template.read_text(encoding="utf-8"))
        narrow = cover.load_font(80, spec["title"]["font"])
        wide = cover.load_font(80, "NotoSans-Bold.ttf")

        assert narrow.getbbox("ШИРИНА СТРОКИ")[2] < wide.getbbox("ШИРИНА СТРОКИ")[2]

    def test_unknown_font_lists_what_is_available(self):
        with pytest.raises(FactoryError) as excinfo:
            cover.load_font(40, "НетТакогоШрифта.ttf")

        assert "PTSansNarrow-Bold.ttf" in str(excinfo.value)


class TestTemplateErrors:
    def test_missing_file_names_the_path(self, template, background, tmp_path):
        missing = tmp_path / "нет-такого.json"

        with pytest.raises(FactoryError) as excinfo:
            cover.render(background, LONG_TITLE, missing)

        assert str(missing) in str(excinfo.value)

    def test_broken_json_is_explained(self, template, background):
        template.write_text("{ это не json", encoding="utf-8")

        with pytest.raises(FactoryError) as excinfo:
            cover.render(background, LONG_TITLE, template)

        assert "не читается" in str(excinfo.value)

    def test_missing_section_is_explained(self, template, background):
        template.write_text(json.dumps({"canvas": {"width": 10, "height": 10}}), encoding="utf-8")

        with pytest.raises(FactoryError) as excinfo:
            cover.render(background, LONG_TITLE, template)

        assert "plate" in str(excinfo.value)

    def test_plate_outside_the_canvas_is_refused(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        spec["plate"]["x"] = 2000
        template.write_text(json.dumps(spec), encoding="utf-8")

        with pytest.raises(FactoryError, match="не помещается"):
            cover.render(background, LONG_TITLE, template)


class TestColours:
    @pytest.mark.parametrize(
        "value,expected",
        [("#FFFFFF", (255, 255, 255)), ("#000000", (0, 0, 0)), ("#D32F2F", (211, 47, 47))],
    )
    def test_hex_is_parsed(self, value, expected):
        assert cover.parse_colour(value) == expected

    def test_broken_colour_is_explained(self):
        with pytest.raises(FactoryError, match="цвет"):
            cover.parse_colour("почти красный")

    @pytest.mark.parametrize("value", ["#FFF", "#FFFFFFFF", "#", "#FFFF"])
    def test_wrong_length_is_refused_even_with_valid_hex_digits(self, value):
        """Иначе #FFFFFFFF молча разберётся как #FFFFFF, отбросив лишнее."""
        with pytest.raises(FactoryError) as excinfo:
            cover.parse_colour(value)

        assert "#RRGGBB" in str(excinfo.value)
