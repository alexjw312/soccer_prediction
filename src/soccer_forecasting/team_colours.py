"""Club colours for the dashboard: badges, and the home/draw/away bar in each club's colours.

Every club has a three-letter code and two kit colours in config/club_colours.csv.
Kit colours were not chosen for charts, so they are checked before use, in light and
dark mode separately:

- visible: at least 1.8:1 contrast with the page surface. That is inside the palette
  checker's "relief" band below 3:1, which is allowed when every block also carries a
  printed label: the bar's parts print their percentages and the names sit above them,
  so colour is never the only cue. It keeps Manchester City's and Coventry's sky blues
  and rules out white and yellow on a white page.
- distinct: the home and away colours must differ by at least 15 (OKLab distance x100)
  for normal vision and 6 under simulated red-green colour blindness (protan and deutan):
  the palette checker's hard floor and its labelled floor. Each must also differ from the
  draw's neutral gray by 12 and 6; the draw block is always the middle one and says
  "Draw" above it, so it needs less separation than the two clubs.

Each side tries its primary colour, then its secondary colour. A kit colour that would
vanish on the page (navy on the dark page, white on the light one) is replaced by a
lighter or darker version with the same hue, or skipped when that would no longer look
like the club. The away side must also stay apart from the home colour; that is how
Arsenal at Liverpool shows Arsenal in navy. Last come white (dark mode) or near-black
(light mode), then the default orange and blue. A test runs every pair of clubs in both
modes.

The colour maths (WCAG contrast, OKLab, Machado colour-blindness matrices) follows the
dataviz skill's validate_palette.js.
"""
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path

SURFACE = {"light": "#FFFFFF", "dark": "#161E27"}
DRAW = {"light": "#8F8D86", "dark": "#8A8983"}
DEFAULT = {"light": {"home": "#2A78D6", "away": "#EB6834"}, "dark": {"home": "#3987E5", "away": "#D95926"}}
NEUTRAL = {"light": "#1F2328", "dark": "#FFFFFF"}
INKS = ("#FFFFFF", "#14202E")  # text inside a coloured block: white or near-black, whichever reads better
MIN_CONTRAST = 1.8
CLUB_FLOORS = (15.0, 6.0)  # normal vision, red-green colour blindness: between the two clubs
DRAW_FLOORS = (12.0, 6.0)  # between a club and the draw's gray

MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
}


@dataclass(frozen=True)
class Club:
    code: str
    primary: str
    secondary: str


HEX = re.compile(r"#[0-9A-F]{6}")
CODE = re.compile(r"[A-Z]{3}")


def load_clubs(root: Path) -> dict[str, Club]:
    """Read config/club_colours.csv. Values end up inside the pages' style rules, so anything
    that is not a #RRGGBB colour or a three-letter code is refused rather than shown."""
    clubs = {}
    with (root / "config" / "club_colours.csv").open(newline="", encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            club = Club(r["code"].strip(), r["primary"].strip().upper(), r["secondary"].strip().upper())
            if not (CODE.fullmatch(club.code) and HEX.fullmatch(club.primary) and HEX.fullmatch(club.secondary)):
                raise ValueError(f"config/club_colours.csv: bad code or colour for {r['team_name']!r}")
            clubs[r["team_name"]] = club
    return clubs


# --- colour maths ---------------------------------------------------------------------

def _srgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _linear(hex_colour: str) -> tuple[float, float, float]:
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in _srgb(hex_colour))


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)


def luminance(hex_colour: str) -> float:
    r, g, b = _linear(hex_colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _oklab(linear_rgb) -> tuple[float, float, float]:
    r, g, b = linear_rgb
    l_ = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _linear_from_oklab(lab) -> tuple[float, float, float]:
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
            -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
            -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)


def _hex_from_linear(rgb) -> str:
    return _hex(12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in (max(0.0, c) for c in rgb))


def _simulate(hex_colour: str, kind: str):
    rgb = _linear(hex_colour)
    return tuple(max(0.0, min(1.0, sum(m * c for m, c in zip(row, rgb)))) for row in MACHADO[kind])


def delta_e(a: str, b: str, kind: str | None = None) -> float:
    """Distance in OKLab x100; kind simulates protan or deutan vision."""
    pa = _oklab(_simulate(a, kind) if kind else _linear(a))
    pb = _oklab(_simulate(b, kind) if kind else _linear(b))
    return 100 * math.dist(pa, pb)


def distinct(a: str, b: str, floors: tuple[float, float] = CLUB_FLOORS) -> bool:
    normal, cvd = floors
    return delta_e(a, b) >= normal and min(delta_e(a, b, "protan"), delta_e(a, b, "deutan")) >= cvd


def visible(colour: str, mode: str) -> bool:
    return contrast(colour, SURFACE[mode]) >= MIN_CONTRAST


def adjusted(colour: str, mode: str) -> str | None:
    """The same hue and strength at a lightness that stands out from the page: lighter in dark
    mode (OKLCH L at least 0.68), darker in light mode (at most 0.50). Strength is reduced only
    as far as needed to stay a displayable colour. None for colours this would not keep
    recognisable: near-grays (white or black would turn into the draw's gray) and colours
    that would have to move a long way (yellow darkened to olive, navy lightened to a new blue)."""
    L, a, b = _oklab(_linear(colour))
    target = max(L, 0.68) if mode == "dark" else min(L, 0.50)
    if math.hypot(a, b) < 0.08 or abs(target - L) > 0.35:
        return None
    for step in range(21):
        keep = 1 - step / 20
        rgb = _linear_from_oklab((target, a * keep, b * keep))
        if all(-1e-6 <= c <= 1 + 1e-6 for c in rgb):
            return _hex_from_linear(rgb)
    return _hex_from_linear(_linear_from_oklab((target, 0.0, 0.0)))


def ink(fill: str) -> str:
    """White or near-black text, whichever has more contrast with the fill."""
    return max(INKS, key=lambda text: contrast(text, fill))


# --- choosing colours -------------------------------------------------------------------

def _candidates(club: Club | None, side: str, mode: str) -> list[str]:
    """A kit colour as it is, or adjusted if it would vanish on this page; never both, so a
    clash is settled by the other kit colour rather than by a lighter shade of the same one."""
    other = "home" if side == "away" else "away"
    defaults = [DEFAULT[mode][side], DEFAULT[mode][other]]
    if club is None:  # a club without colours gets the site's own blue or orange
        return defaults + [NEUTRAL[mode]]
    own = [c if visible(c, mode) else adjusted(c, mode) for c in (club.primary, club.secondary)]
    return [c for c in own if c] + [NEUTRAL[mode]] + defaults


def _first(options: list[str], mode: str, home: str | None = None) -> str:
    for colour in options:
        if (visible(colour, mode) and distinct(colour, DRAW[mode], DRAW_FLOORS)
                and (home is None or distinct(colour, home))):
            return colour
    raise ValueError("no usable colour")  # unreachable: the defaults always qualify (tested)


def match_colours(home: Club | None, away: Club | None) -> dict[str, dict[str, str]]:
    """{mode: {"home", "away", "home_ink", "away_ink"}} for light and dark mode."""
    result = {}
    for mode in ("light", "dark"):
        h = _first(_candidates(home, "home", mode), mode)
        a = _first(_candidates(away, "away", mode), mode, home=h)
        result[mode] = {"home": h, "away": a, "home_ink": ink(h), "away_ink": ink(a)}
    return result


def badge(club: Club | None, name: str) -> tuple[str, str, str]:
    """(code, background, text colour) for a club's round badge; gray with initials when unlisted."""
    if club is None:
        words = [w for w in name.replace("&", " ").split() if w[:1].isalpha()]
        code = ("".join(w[0] for w in words[:3]) if len(words) > 1 else name[:3]).upper()
        return code, "#8F8D86", "#FFFFFF"
    text = club.secondary if contrast(club.secondary, club.primary) >= 4.5 else ink(club.primary)
    return club.code, club.primary, text
