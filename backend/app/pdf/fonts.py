"""Font discovery, web @font-face generation and static-instance creation for export.

Variable fonts (wght axis) give a continuous weight slider. Static families expose
exactly the weights they ship with.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from fontTools.ttLib import TTFont

from . import PYMU_LOCK

log = logging.getLogger("pdft.fonts")
FONT_EXT = {".ttf", ".otf"}
PREFERRED = ("Noto Sans KR", "NanumGothic", "Nanum Gothic", "Noto Sans", "DejaVu Sans")


@dataclass
class FontFile:
    id: str
    path: Path
    family: str
    weight: int
    variable: bool
    wmin: int = 400
    wmax: int = 400


class _Combined:
    """Latin chars from `latin`, everything else from `base`."""

    def __init__(self, latin, base):
        self.latin, self.base = latin, base

    def text_length(self, s: str, fontsize: float = 11) -> float:
        return sum((self.latin if ord(c) < 0x250 else self.base).text_length(c, fontsize) for c in s)


class FontManager:
    def __init__(self, dirs: list[Path], cache_dir: Path):
        self.dirs = dirs
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.families: dict[str, list[FontFile]] = {}
        self.by_id: dict[str, FontFile] = {}
        self._measure: dict[str, object] = {}
        self.scan()

    # ------------------------------------------------------------------ scan
    def _inspect(self, p: Path) -> FontFile | None:
        try:
            f = TTFont(p, lazy=True, fontNumber=0)
            os2 = f["OS/2"] if "OS/2" in f else None
            italic = bool(os2 and os2.fsSelection & 1) or bool(f["head"].macStyle & 2)
            if italic or "cmap" not in f:
                return None
            family = f["name"].getBestFamilyName() or p.stem
            weight = max(100, min(900, int(os2.usWeightClass))) if os2 else 400
            var, wmin, wmax = False, weight, weight
            if "fvar" in f:
                for ax in f["fvar"].axes:
                    if ax.axisTag == "wght":
                        var, wmin, wmax = True, int(ax.minValue), int(ax.maxValue)
            fid = hashlib.sha1(str(p).encode()).hexdigest()[:12]
            f.close()
            return FontFile(fid, p, family, weight, var, wmin, wmax)
        except Exception as e:  # noqa: BLE001
            log.debug("skip font %s: %s", p, e)
            return None

    def scan(self) -> None:
        groups: dict[str, list[FontFile]] = {}
        seen: set[Path] = set()
        for d in self.dirs:
            if not d.exists():
                continue
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() in FONT_EXT and p.is_file() and p.resolve() not in seen:
                    seen.add(p.resolve())
                    if (ff := self._inspect(p)):
                        groups.setdefault(ff.family, []).append(ff)
        self.families, self.by_id = {}, {}
        for fam, files in groups.items():
            variable = [x for x in files if x.variable]
            keep = variable[:1] if variable else files
            self.families[fam] = keep
            for x in keep:
                self.by_id[x.id] = x
        log.info("fonts: %d families", len(self.families))

    # ------------------------------------------------------------- queries
    @staticmethod
    def weights_of(files: list[FontFile]) -> list[int]:
        v = next((x for x in files if x.variable), None)
        if v:
            ws = [w for w in range(100, 1000, 100) if v.wmin <= w <= v.wmax]
            return ws or [v.weight]
        return sorted({x.weight for x in files})

    def public(self) -> list[dict]:
        out = [
            {"family": fam, "variable": any(x.variable for x in files), "weights": self.weights_of(files)}
            for fam, files in self.families.items()
        ]
        return sorted(out, key=lambda x: (x["family"] not in PREFERRED, x["family"].lower()))

    def default_family(self) -> str | None:
        for name in PREFERRED:
            if name in self.families:
                return name
        return next(iter(self.families), None)

    def has(self, family: str | None) -> bool:
        return bool(family) and family in self.families

    def resolve(self, family: str | None, default: str | None) -> str | None:
        if self.has(family):
            return family
        if self.has(default):
            return default
        return self.default_family()

    def nearest(self, family: str, weight: int) -> FontFile:
        files = self.families[family]
        return min(files, key=lambda x: (abs(x.weight - weight), -x.weight)) if not files[0].variable else files[0]

    def snap_weight(self, family: str, weight: int) -> int:
        ws = self.weights_of(self.families[family])
        return min(ws, key=lambda w: abs(w - weight))

    def face_css(self) -> str:
        out = []
        for fam, files in self.families.items():
            for x in files:
                fmt = "opentype" if x.path.suffix.lower() == ".otf" else "truetype"
                w = f"{x.wmin} {x.wmax}" if x.variable else str(x.weight)
                name = fam.replace('"', "")
                out.append(
                    f'@font-face{{font-family:"{name}";src:url("/api/fonts/file/{x.id}") format("{fmt}");'
                    f"font-weight:{w};font-style:normal;font-display:swap}}"
                )
        return "\n".join(out)

    # -------------------------------------------------------------- layout
    def measure_font(self, family: str, weight: int):
        """Object with .text_length(str, fontsize) for layout measuring.

        Variable CJK fonts often have a Thin default master, so Latin glyphs are measured on a
        small instance at the requested weight; CJK glyphs have weight-independent advances.
        """
        ff = self.nearest(family, weight)
        with PYMU_LOCK:
            base = self._font(ff.path)
            if not ff.variable or max(ff.wmin, min(ff.wmax, weight)) == self.default_weight(ff):
                return base
            key = f"{ff.id}|{weight}"
            if key not in self._measure:
                latin = self._font(self.materialize(family, weight, set(), self.cache_dir))
                self._measure[key] = _Combined(latin, base)
            return self._measure[key]

    def _font(self, path: Path) -> pymupdf.Font:
        key = str(path)
        if key not in self._measure:
            self._measure[key] = pymupdf.Font(fontfile=key)
        return self._measure[key]

    @staticmethod
    def default_weight(ff: FontFile) -> int:
        try:
            f = TTFont(ff.path, lazy=True)
            for a in f["fvar"].axes:
                if a.axisTag == "wght":
                    return int(a.defaultValue)
        except Exception:  # noqa: BLE001
            pass
        return 400

    # -------------------------------------------------------------- export
    def materialize(self, family: str, weight: int, chars: set[str], out_dir: Path) -> Path:
        """Return a static font file for (family, weight) inside out_dir (used by MuPDF HTML)."""
        ff = self.nearest(family, weight)
        if not ff.variable:
            dst = out_dir / f"{ff.id}{ff.path.suffix.lower()}"
            if not dst.exists():
                try:
                    os.symlink(ff.path, dst)
                except OSError:
                    shutil.copy(ff.path, dst)
            return dst
        from fontTools import subset
        from fontTools.varLib import instancer

        w = max(ff.wmin, min(ff.wmax, weight))
        text = "".join(sorted(chars | set(chr(c) for c in range(32, 127))))
        key = hashlib.sha1(f"{ff.id}|{w}|{text}".encode()).hexdigest()[:16]
        cached = self.cache_dir / f"{key}.ttf"
        if not cached.exists():
            font = TTFont(ff.path)
            opts = subset.Options()
            opts.layout_features = ["*"]
            opts.notdef_outline = True
            opts.glyph_names = False
            opts.hinting = False
            sub = subset.Subsetter(opts)
            sub.populate(text=text)
            sub.subset(font)
            loc = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
            loc["wght"] = w
            instancer.instantiateVariableFont(font, loc, inplace=True)
            tmp = cached.with_suffix(".tmp")
            font.save(tmp)
            tmp.replace(cached)
        dst = out_dir / f"{key}.ttf"
        if not dst.exists():
            shutil.copy(cached, dst)
        return dst
