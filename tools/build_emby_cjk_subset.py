"""Build the redistributable GB2312 subset bundled by EmbyLibraryCover."""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont


def cjk_unicodes() -> set[int]:
    codepoints = set(range(0x20, 0x7F))
    for lead in range(0xA1, 0xF8):
        for trail in range(0xA1, 0xFF):
            try:
                text = bytes((lead, trail)).decode("gb2312")
            except UnicodeDecodeError:
                continue
            codepoints.update(ord(char) for char in text)
    for lead in range(0x81, 0xFF):
        for trail in range(0x40, 0xFF):
            if trail == 0x7F:
                continue
            try:
                text = bytes((lead, trail)).decode("gbk")
            except UnicodeDecodeError:
                continue
            codepoints.update(ord(char) for char in text)
    codepoints.update(range(0x3000, 0x3040))
    return codepoints


def set_name(record, value: str) -> None:
    if record.isUnicode():
        record.string = value.encode("utf-16-be")
    elif record.platformID == 1:
        record.string = value.encode("mac_roman", errors="replace")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_emby_cjk_subset.py INPUT.otf OUTPUT.otf")
    source, target = map(Path, sys.argv[1:])
    options = subset.Options()
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    font = subset.load_font(str(source), options)
    worker = subset.Subsetter(options=options)
    worker.populate(unicodes=cjk_unicodes())
    worker.subset(font)
    subset.save_font(font, str(target), options)

    renamed = TTFont(target)
    replacements = {
        1: "MoviePilot CJK SC",
        2: "Bold",
        3: "MoviePilot CJK SC Bold 1.0",
        4: "MoviePilot CJK SC Bold",
        6: "MoviePilotCJKSC-Bold",
    }
    for record in renamed["name"].names:
        if record.nameID in replacements:
            set_name(record, replacements[record.nameID])
    renamed.save(target)
    renamed.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
