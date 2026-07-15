from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat


def _load_font(path: str, size: int, fallbacks: tuple[str, ...]):
    for candidate in (path, *fallbacks):
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


def _multiline(draw, xy, text: str, font, fill, spacing: int = 12, letter_spacing: int = 0):
    x, y = xy
    for line in text.splitlines():
        cursor = x
        for char in line:
            draw.text((cursor, y), char, font=font, fill=fill)
            cursor += draw.textlength(char, font=font) + letter_spacing
        box = draw.textbbox((x, y), line or " ", font=font)
        y += box[3] - box[1] + spacing


class CoverRenderer:
    SIZE = (1920, 1080)

    def __init__(self, font_zh: str = "", font_en: str = "", output_format: str = "jpg", quality: int = 92):
        self.font_zh = font_zh
        self.font_en = font_en
        self.output_format = output_format
        self.quality = max(70, min(int(quality), 100))

    def render(
        self,
        style: str,
        title: dict[str, str],
        posters: list[Image.Image],
        backdrop: Image.Image | None,
        output_path: Path,
    ) -> Path:
        if not posters:
            raise ValueError("POSTERS_EMPTY")
        image = self._style_2(title, posters) if style == "style_2" else self._style_1(title, posters, backdrop)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.output_format == "png":
            image.save(output_path, "PNG", optimize=True)
        else:
            image.convert("RGB").save(output_path, "JPEG", quality=self.quality, optimize=True)
        return output_path

    def _fonts(self, zh_size: int, en_size: int):
        zh = _load_font(self.font_zh, zh_size, (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ))
        en = _load_font(self.font_en, en_size, (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ))
        return zh, en

    def _style_1(self, title: dict[str, str], posters: list[Image.Image], backdrop: Image.Image | None) -> Image.Image:
        base = _fit(backdrop or posters[0], self.SIZE).convert("RGBA")
        base.alpha_composite(Image.new("RGBA", self.SIZE, (0, 0, 0, 110)))
        self._left_gradient(base, 1000, 180)
        self._bottom_gradient(base, 155)
        self._snow(base)
        draw = ImageDraw.Draw(base)
        zh_font, en_font = self._fonts(150, 70)
        draw.text((100, 100), title["zh"], font=zh_font, fill=(255, 255, 255, 245))
        _multiline(draw, (110, 260), title["en"], en_font, (255, 255, 255, 238), 12, 10)

        poster_size = (280, 420)
        selected = posters[:6]
        width = len(selected) * poster_size[0] + max(0, len(selected) - 1) * 20
        start_x = max(40, (self.SIZE[0] - width) // 2)
        for index, poster in enumerate(selected):
            x = start_x + index * 300
            base.alpha_composite(self._shadow(poster_size, 10, 45), (x - 10, 604))
            base.alpha_composite(_rounded(poster, poster_size), (x, 610))
        return base.convert("RGB")

    def _style_2(self, title: dict[str, str], posters: list[Image.Image]) -> Image.Image:
        color = self._dominant(posters[0])
        canvas = Image.new("RGBA", self.SIZE, color + (255,))
        canvas.alpha_composite(Image.new("RGBA", self.SIZE, (0, 0, 0, 70)))
        wall = self._poster_wall(posters)
        rotated = wall.rotate(-15, expand=True, resample=Image.Resampling.BICUBIC)
        canvas.alpha_composite(rotated, (int(1600 - rotated.width / 2), int(540 - rotated.height / 2)))

        draw = ImageDraw.Draw(canvas)
        zh_font, en_font = self._fonts(180, 50)
        x, y = 100, 390
        draw.text((x, y), title["zh"], font=zh_font, fill=(255, 255, 255, 245))
        box = draw.textbbox((x, y), title["zh"], font=zh_font)
        en_y = box[3] + 24
        draw.rounded_rectangle((x, en_y + 4, x + 10, en_y + 110), radius=5, fill=(255, 140, 0, 255))
        _multiline(draw, (x + 28, en_y), title["en"], en_font, (255, 255, 255, 225), 12, 6)
        return canvas.convert("RGB")

    def _poster_wall(self, posters: list[Image.Image]) -> Image.Image:
        size, sx, sy, stagger = (400, 600), 10, 10, 180
        wall = Image.new("RGBA", (3 * size[0] + 2 * sx + 80, 3 * size[1] + 2 * sy + 2 * stagger + 80), (0, 0, 0, 0))
        for index, poster in enumerate(posters[:9]):
            col, row = index % 3, index // 3
            x = 40 + col * (size[0] + sx)
            y = 40 + row * (size[1] + sy) + col * stagger
            wall.alpha_composite(self._shadow(size, 16, 70), (x - 16, y - 8))
            wall.alpha_composite(_rounded(poster, size, 6), (x, y))
        return wall

    def _left_gradient(self, canvas: Image.Image, width: int, max_alpha: int):
        layer = Image.new("RGBA", self.SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for x in range(width):
            draw.line((x, 0, x, self.SIZE[1]), fill=(0, 0, 0, int(max_alpha * (1 - x / width) ** 1.4)))
        canvas.alpha_composite(layer)

    def _bottom_gradient(self, canvas: Image.Image, max_alpha: int):
        layer = Image.new("RGBA", self.SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        start = self.SIZE[1] // 2
        for y in range(start, self.SIZE[1]):
            alpha = int(max_alpha * ((y - start) / (self.SIZE[1] - start)) ** 1.7)
            draw.line((0, y, self.SIZE[0], y), fill=(0, 0, 0, alpha))
        canvas.alpha_composite(layer)

    def _snow(self, canvas: Image.Image):
        draw = ImageDraw.Draw(canvas)
        rng = random.Random(20260708)
        for _ in range(200):
            x, y, radius = rng.randint(0, self.SIZE[0]), rng.randint(0, self.SIZE[1]), rng.randint(2, 11)
            alpha = rng.randint(60, 190)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(235, 245, 248, alpha))

    @staticmethod
    def _shadow(size: tuple[int, int], blur: int, alpha: int) -> Image.Image:
        shadow = Image.new("RGBA", (size[0] + blur * 2, size[1] + blur * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (blur, blur, blur + size[0], blur + size[1]), radius=10, fill=(0, 0, 0, alpha)
        )
        return shadow.filter(ImageFilter.GaussianBlur(blur))

    @staticmethod
    def _dominant(image: Image.Image) -> tuple[int, int, int]:
        mean = ImageStat.Stat(image.convert("RGB").resize((1, 1), Image.Resampling.BOX)).mean
        return tuple(max(20, min(90, int(channel * 0.55))) for channel in mean)
