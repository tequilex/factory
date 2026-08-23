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


class TestPlate:
    def test_plate_is_filled_with_its_colour(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        image = build(template, background)

        corner = (plate["x"] + 4, plate["y"] + 4)
        assert image.getpixel(corner)[:3] == cover.parse_colour(plate["color"])


class TestTitle:
    def test_long_cyrillic_title_fits_inside_the_plate(self, template, background):
        """Требование спеки, ради которого этот модуль и существует."""
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        padding = plate["padding"]
        image = build(template, background, LONG_TITLE)

        box = cover.text_bounds(image, cover.parse_colour(spec["title"]["color"]))

        assert box is not None, "текст не отрисован"
        left, top, right, bottom = box
        assert left >= plate["x"] + padding - 1
        assert top >= plate["y"] + padding - 1
        assert right <= plate["x"] + plate["width"] - padding + 1
        assert bottom <= plate["y"] + plate["height"] - padding + 1

    def test_short_title_is_set_larger_than_a_long_one(self, template, background):
        """Автоподбор кегля: короткий заголовок должен занимать плашку крупнее."""
        spec = json.loads(template.read_text(encoding="utf-8"))

        short = cover.fit_font_size(SHORT_TITLE, spec)
        long = cover.fit_font_size(LONG_TITLE, spec)

        assert short > long

    def test_chosen_size_is_the_largest_that_fits(self, template, background):
        """Подбор обязан брать самый крупный подходящий кегль, а не любой подходящий.

        Без этой проверки подмена «пробовать только максимум» проходит незаметно:
        функция сваливается на минимальный кегль, тесты остаются зелёными, а
        заголовки в бою рисуются мелкими.
        """
        spec = json.loads(template.read_text(encoding="utf-8"))
        plate = spec["plate"]
        padding = plate["padding"]
        available_width = plate["width"] - 2 * padding
        available_height = plate["height"] - 2 * padding
        spacing = spec["title"].get("line_spacing", 1.1)

        chosen = cover.fit_font_size(LONG_TITLE, spec)
        assert chosen < spec["title"]["size_max"], "подобрать нечего: заголовок влез в максимум"

        bigger = cover.load_font(chosen + 1)
        width, height = cover._block_size(
            cover.wrap(LONG_TITLE, bigger, available_width), bigger, spacing
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
        font = cover.load_font(cover.fit_font_size(LONG_TITLE, spec))
        available = spec["plate"]["width"] - 2 * spec["plate"]["padding"]

        lines = cover.wrap(LONG_TITLE, font, available)

        assert " ".join(lines) == LONG_TITLE, "слова переставлены или разорваны"
        assert len(lines) > 1, "длинный заголовок должен переноситься"

    def test_a_single_unbreakable_word_does_not_crash(self, template, background):
        """Слово длиннее строки: кегль опустится до минимума, но сборка пройдёт."""
        monster = "Достопримечательность" * 3

        image = build(template, background, monster)

        assert image.size == (1080, 1350)

    def test_title_is_actually_drawn(self, template, background):
        spec = json.loads(template.read_text(encoding="utf-8"))
        with_text = build(template, background, LONG_TITLE)
        without = build(template, background, "")

        assert cover.text_bounds(with_text, cover.parse_colour(spec["title"]["color"]))
        assert cover.text_bounds(without, cover.parse_colour(spec["title"]["color"])) is None


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
