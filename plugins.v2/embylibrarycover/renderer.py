from __future__ import annotations

import random
import colorsys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat


DEFAULT_RENDER_CONFIG: dict[str, Any] = {
    "output_size": (1920, 1080),
    "font_zh_path": "", "font_en_path": "",
    "output_format": "jpg", "jpeg_quality": 92,
    "s1_text_pos_zh": (100, 100), "s1_text_pos_en": (110, 260),
    "s1_font_size_zh": 150, "s1_font_size_en": 70,
    "s1_title_gap": 20, "s1_en_letter_spacing": 10, "s1_en_line_spacing": 12,
    "s1_poster_count": 6, "s1_poster_size": (280, 420),
    "s1_poster_spacing": 20, "s1_poster_y_pos": 610,
    "s1_background_blur_enable": False, "s1_blur_percent": 0,
    "s1_overlay_alpha": 110, "s1_gradient_width": 1000,
    "s1_gradient_max_alpha": 180, "s1_bottom_gradient_enable": True,
    "s1_bottom_gradient_max_alpha": 155, "s1_snow_enable": True,
    "s1_snow_density": 60, "s1_snow_radius_min": 2, "s1_snow_radius_max": 8,
    "s1_snow_alpha_min": 120, "s1_snow_alpha_max": 200,
    "s1_snow_seed": 20260708,
    "s2_poster_count": 9, "s2_poster_size": (400, 600),
    "s2_poster_spacing_x": 10, "s2_poster_spacing_y": 10,
    "s2_poster_stagger": 180, "s2_poster_rotation": -15,
    "s2_poster_center": (1600, 540), "s2_text_pos": (100, 390),
    "s2_font_size_zh": 180, "s2_font_size_en": 50,
    "s2_title_gap": 24, "s2_en_letter_spacing": 6, "s2_en_line_spacing": 12,
    "s2_accent_bar_enable": True, "s2_accent_bar_color": (255, 140, 0),
    "s2_bg_auto_color": True, "s2_bg_default_color": (30, 30, 35),
    "s3_poster_count": 4, "s3_text_pos": (160, 350),
    "s3_font_size_zh": 150, "s3_font_size_en": 46,
    "s3_title_gap": 28, "s3_en_letter_spacing": 8, "s3_en_line_spacing": 10,
    "s3_overlay_alpha": 55, "s3_left_gradient_alpha": 235,
}


def _font(path: str, size: int, fallbacks: tuple[str, ...]):
    if path:
        if not Path(path).is_file():
            raise ValueError("FONT_FILE_MISSING")
        try:
            return ImageFont.truetype(path, size)
        except OSError as exc:
            raise ValueError("FONT_LOAD_FAILED") from exc
    for candidate in fallbacks:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)


def _rounded(image: Image.Image, size: tuple[int, int], radius: int = 8) -> Image.Image:
    poster = _fit(image, size).convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    poster.putalpha(mask)
    return poster


def _multiline(draw, xy, text: str, font, fill, spacing: int, letter_spacing: int):
    x, y = xy
    for line in text.splitlines():
        cursor = x
        for char in line:
            draw.text((cursor, y), char, font=font, fill=fill)
            cursor += draw.textlength(char, font=font) + letter_spacing
        box = draw.textbbox((x, y), line or " ", font=font)
        y += box[3] - box[1] + spacing


def _english_title_position(draw, zh_xy, en_x: int, zh_text: str, zh_font, title_gap: int) -> tuple[int, int]:
    box = draw.textbbox(zh_xy, zh_text or " ", font=zh_font)
    return int(en_x), int(box[3] + title_gap)


class CoverRenderer:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**DEFAULT_RENDER_CONFIG, **(config or {})}
        self.size = tuple(self.config["output_size"])

    def poster_count(self, style: str) -> int:
        if style == "style_3":
            return int(self.config["s3_poster_count"])
        return int(self.config["s2_poster_count"] if style == "style_2" else self.config["s1_poster_count"])

    def validate_fonts(self) -> None:
        self._fonts(int(self.config["s1_font_size_zh"]), int(self.config["s1_font_size_en"]))

    def render(self, style: str, title: dict[str, str], posters: list[Image.Image], backdrop: Image.Image | None, output_path: Path) -> Path:
        if not posters:
            raise ValueError("POSTERS_EMPTY")
        if style == "style_3":
            image = self._style_3(title, posters, backdrop)
        elif style == "style_2":
            image = self._style_2(title, posters)
        else:
            image = self._style_1(title, posters, backdrop)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config["output_format"] == "png":
            image.save(output_path, "PNG", optimize=True)
        else:
            image.convert("RGB").save(
                output_path, "JPEG", quality=int(self.config["jpeg_quality"]), optimize=True
            )
        return output_path

    def _fonts(self, zh_size: int, en_size: int):
        zh = _font(self.config["font_zh_path"], zh_size, (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ))
        en = _font(self.config["font_en_path"], en_size, (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ))
        return zh, en

    def _style_1(self, title: dict[str, str], posters: list[Image.Image], backdrop: Image.Image | None) -> Image.Image:
        c = self.config
        base = _fit(backdrop or posters[0], self.size)
        if c["s1_background_blur_enable"]:
            base = base.filter(ImageFilter.GaussianBlur(max(0, int(c["s1_blur_percent"] / 100 * 40))))
        canvas = base.convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", self.size, (0, 0, 0, c["s1_overlay_alpha"])))
        self._left_gradient(canvas, c["s1_gradient_width"], c["s1_gradient_max_alpha"])
        if c["s1_bottom_gradient_enable"]:
            self._bottom_gradient(canvas, c["s1_bottom_gradient_max_alpha"])
        if c["s1_snow_enable"]:
            self._snow(canvas)
        draw = ImageDraw.Draw(canvas)
        zh_font, en_font = self._fonts(c["s1_font_size_zh"], c["s1_font_size_en"])
        zh_xy = c["s1_text_pos_zh"]
        draw.text(zh_xy, title["zh"], font=zh_font, fill=(255, 255, 255, 245))
        en_xy = _english_title_position(
            draw, zh_xy, c["s1_text_pos_en"][0], title["zh"], zh_font, c["s1_title_gap"]
        )
        _multiline(draw, en_xy, title["en"], en_font, (255, 255, 255, 238), c["s1_en_line_spacing"], c["s1_en_letter_spacing"])
        size = tuple(c["s1_poster_size"])
        selected = posters[:c["s1_poster_count"]]
        width = len(selected) * size[0] + max(0, len(selected) - 1) * c["s1_poster_spacing"]
        start_x = max(0, (self.size[0] - width) // 2)
        for index, poster in enumerate(selected):
            x = start_x + index * (size[0] + c["s1_poster_spacing"])
            y = c["s1_poster_y_pos"]
            canvas.alpha_composite(self._shadow(size, 10, 45), (x - 10, y - 6))
            canvas.alpha_composite(_rounded(poster, size), (x, y))
        return canvas.convert("RGB")

    def _style_2(self, title: dict[str, str], posters: list[Image.Image]) -> Image.Image:
        c = self.config
        color = self._dominant(posters[0]) if c["s2_bg_auto_color"] else tuple(c["s2_bg_default_color"])
        canvas = Image.new("RGBA", self.size, color + (255,))
        canvas.alpha_composite(Image.new("RGBA", self.size, (0, 0, 0, 70)))
        wall = self._poster_wall(posters)
        rotated = wall.rotate(c["s2_poster_rotation"], expand=True, resample=Image.Resampling.BICUBIC)
        cx, cy = c["s2_poster_center"]
        canvas.alpha_composite(rotated, (int(cx - rotated.width / 2), int(cy - rotated.height / 2)))
        draw = ImageDraw.Draw(canvas)
        zh_font, en_font = self._fonts(c["s2_font_size_zh"], c["s2_font_size_en"])
        x, y = c["s2_text_pos"]
        draw.text((x, y), title["zh"], font=zh_font, fill=(255, 255, 255, 245))
        _, en_y = _english_title_position(draw, (x, y), x, title["zh"], zh_font, c["s2_title_gap"])
        if c["s2_accent_bar_enable"]:
            draw.rounded_rectangle((x, en_y + 4, x + 10, en_y + 110), radius=5, fill=tuple(c["s2_accent_bar_color"]) + (255,))
            x += 28
        _multiline(draw, (x, en_y), title["en"], en_font, (255, 255, 255, 225), c["s2_en_line_spacing"], c["s2_en_letter_spacing"])
        return canvas.convert("RGB")

    def _style_3(self, title: dict[str, str], posters: list[Image.Image], backdrop: Image.Image | None) -> Image.Image:
        c = self.config
        if backdrop:
            base = _fit(backdrop, self.size)
        else:
            base = self._poster_strip(posters[:c["s3_poster_count"]])
        canvas = base.convert("RGBA")
        canvas.alpha_composite(Image.new("RGBA", self.size, (0, 0, 0, c["s3_overlay_alpha"])))

        gradient = Image.new("RGBA", self.size, (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        gradient_width = max(1, int(self.size[0] * 0.72))
        for x in range(gradient_width):
            ratio = 1 - x / gradient_width
            alpha = int(c["s3_left_gradient_alpha"] * ratio ** 1.65)
            gradient_draw.line((x, 0, x, self.size[1]), fill=(5, 7, 9, alpha))
        canvas.alpha_composite(gradient)
        self._bottom_gradient(canvas, 115)

        draw = ImageDraw.Draw(canvas)
        accent = self._accent_color(base)

        zh_font, en_font = self._fonts(c["s3_font_size_zh"], c["s3_font_size_en"])
        x, y = c["s3_text_pos"]
        draw.rounded_rectangle((x, y - 42, x + 125, y - 34), radius=4, fill=accent + (235,))
        draw.text((x, y), title["zh"], font=zh_font, fill=(255, 250, 240, 250))
        en_xy = _english_title_position(draw, (x, y), x + 4, title["zh"], zh_font, c["s3_title_gap"])
        _multiline(
            draw, en_xy, title["en"], en_font, (238, 222, 194, 238),
            c["s3_en_line_spacing"], c["s3_en_letter_spacing"],
        )
        return canvas.convert("RGB")

    def _poster_strip(self, posters: list[Image.Image]) -> Image.Image:
        if not posters:
            raise ValueError("POSTERS_EMPTY")
        count = min(4, len(posters))
        strip = Image.new("RGB", self.size, (18, 20, 24))
        segment_width = (self.size[0] + count - 1) // count
        for index, poster in enumerate(posters[:count]):
            segment = _fit(poster, (segment_width, self.size[1]))
            strip.paste(segment, (index * segment_width, 0))
        return strip

    def _poster_wall(self, posters: list[Image.Image]) -> Image.Image:
        c = self.config
        size = tuple(c["s2_poster_size"])
        count = c["s2_poster_count"]
        cols, rows = 3, max(1, (count + 2) // 3)
        width = cols * size[0] + (cols - 1) * c["s2_poster_spacing_x"]
        height = rows * size[1] + (rows - 1) * c["s2_poster_spacing_y"] + c["s2_poster_stagger"] * (cols - 1)
        wall = Image.new("RGBA", (width + 80, height + 80), (0, 0, 0, 0))
        for index, poster in enumerate(posters[:count]):
            col, row = index % cols, index // cols
            x = 40 + col * (size[0] + c["s2_poster_spacing_x"])
            y = 40 + row * (size[1] + c["s2_poster_spacing_y"]) + col * c["s2_poster_stagger"]
            wall.alpha_composite(self._shadow(size, 16, 70), (x - 16, y - 8))
            wall.alpha_composite(_rounded(poster, size, 6), (x, y))
        return wall

    def _left_gradient(self, canvas: Image.Image, width: int, max_alpha: int):
        width = max(1, min(width, self.size[0]))
        layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for x in range(width):
            draw.line((x, 0, x, self.size[1]), fill=(0, 0, 0, int(max_alpha * (1 - x / width) ** 1.4)))
        canvas.alpha_composite(layer)

    def _bottom_gradient(self, canvas: Image.Image, max_alpha: int):
        layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        start = self.size[1] // 2
        for y in range(start, self.size[1]):
            alpha = int(max_alpha * ((y - start) / max(1, self.size[1] - start)) ** 1.7)
            draw.line((0, y, self.size[0], y), fill=(0, 0, 0, alpha))
        canvas.alpha_composite(layer)

    def _snow(self, canvas: Image.Image):
        c = self.config
        draw = ImageDraw.Draw(canvas)
        rng = random.Random(c["s1_snow_seed"])
        for _ in range(c["s1_snow_density"]):
            x, y = rng.randint(0, self.size[0]), rng.randint(0, self.size[1])
            radius = rng.randint(c["s1_snow_radius_min"], c["s1_snow_radius_max"])
            alpha = rng.randint(c["s1_snow_alpha_min"], c["s1_snow_alpha_max"])
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(235, 245, 248, alpha))

    @staticmethod
    def _shadow(size: tuple[int, int], blur: int, alpha: int) -> Image.Image:
        shadow = Image.new("RGBA", (size[0] + blur * 2, size[1] + blur * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((blur, blur, blur + size[0], blur + size[1]), radius=10, fill=(0, 0, 0, alpha))
        return shadow.filter(ImageFilter.GaussianBlur(blur))

    @staticmethod
    def _dominant(image: Image.Image) -> tuple[int, int, int]:
        mean = ImageStat.Stat(image.convert("RGB").resize((1, 1), Image.Resampling.BOX)).mean
        return tuple(max(20, min(90, int(channel * 0.55))) for channel in mean)

    @staticmethod
    def _accent_color(image: Image.Image) -> tuple[int, int, int]:
        mean = ImageStat.Stat(image.convert("RGB").resize((1, 1), Image.Resampling.BOX)).mean
        red, green, blue = (channel / 255 for channel in mean)
        hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
        saturation = max(0.38, min(0.78, saturation * 1.35))
        value = max(0.72, min(0.92, value * 1.18))
        result = colorsys.hsv_to_rgb(hue, saturation, value)
        return tuple(max(0, min(255, round(channel * 255))) for channel in result)
