"""Render the skills-router-mcp usability demo (1920x1080, 30 fps) to MP4.

Every frame is a pure function of the time t. The terminal text is real output of
`skills-router` v1.0.0 captured in a throwaway HOME holding 11 sample skills across Claude Code,
Gemini CLI and Codex (PATH without `claude`); only the sandbox path was replaced by `~` and some
lines were omitted. The agent's last line in scene 3 is narration, not tool output.

    uv run --no-project --with pillow python docs/demo/render_demo.py OUT.mp4
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
HERE = Path(__file__).resolve().parent
LOGO = HERE.parent.parent / "skills-router-mcp-logo.png"

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
SANS_B = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"

BG0, BG1 = (11, 19, 36), (5, 8, 16)
GRID = (22, 35, 58)
BORDER = (30, 51, 80)
TERM = (8, 13, 24)
TEXT = (226, 232, 240)
DIM = (100, 116, 139)
MUTED = (148, 163, 184)
EMERALD = (52, 211, 153)
CYAN = (34, 211, 238)
AMBER = (251, 191, 36)

_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


def ease(t: float, a: float, b: float) -> float:
    """0 before a, 1 after b, smooth cubic in between."""
    if b <= a:
        return 1.0 if t >= b else 0.0
    x = clamp((t - a) / (b - a))
    return x * x * (3 - 2 * x)


def mix(c1, c2, k: float):
    return tuple(int(round(a + (b - a) * k)) for a, b in zip(c1, c2))


# ---------------------------------------------------------------- background

def _background() -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(0, W, 4):
            k = (x / W + y / H) / 2
            c = mix(BG0, BG1, k)
            for dx in range(4):
                px[x + dx, y] = c
    d = ImageDraw.Draw(img)
    for x in range(0, W, 48):
        d.line([(x, 0), (x, H)], fill=mix(GRID, BG1, 0.45))
    for y in range(0, H, 48):
        d.line([(0, y), (W, y)], fill=mix(GRID, BG1, 0.45))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W * 0.55, -300, W * 1.25, 500], fill=(8, 60, 50))
    gd.ellipse([-400, H * 0.6, 500, H * 1.4], fill=(6, 40, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    return _add(img, glow)


def _add(a: Image.Image, b: Image.Image) -> Image.Image:
    from PIL import ImageChops

    return ImageChops.add(a, b)


BACKGROUND = _background()
_logo_cache: dict[int, Image.Image] = {}


def logo(size: int) -> Image.Image:
    if size not in _logo_cache:
        _logo_cache[size] = Image.open(LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)
    return _logo_cache[size]


# ---------------------------------------------------------------- text helpers

def text_w(s: str, f) -> float:
    return f.getlength(s)


def draw_center(d: ImageDraw.ImageDraw, y: float, s: str, f, fill) -> None:
    d.text(((W - text_w(s, f)) / 2, y), s, font=f, fill=fill)


RULES = [
    (re.compile(r"^\[dry-run\]"), DIM),
    (re.compile(r"(Claude Code|Gemini CLI|Codex CLI)(?=:)"), CYAN),
    (re.compile(r"Skills importadas: \d+|duplicadas idênticas: \d+|Índice: \d+ skills|Itens devolvidos ao lugar original: \d+"), EMERALD),
    (re.compile(r"Nada foi alterado \(dry-run\)\.|Pronto\..*"), EMERALD),
    (re.compile(r"\"score\": [0-9.]+|\"name\": \"security-audit\""), EMERALD),
    (re.compile(r"\[skills-router\]"), AMBER),
]


def colorize(line: str, base=TEXT) -> list[tuple[str, tuple]]:
    colors = [base] * len(line)
    for rx, c in RULES:
        for m in rx.finditer(line):
            for i in range(m.start(), m.end()):
                colors[i] = c
    segs: list[tuple[str, tuple]] = []
    for ch, c in zip(line, colors):
        if segs and segs[-1][1] == c:
            segs[-1] = (segs[-1][0] + ch, c)
        else:
            segs.append((ch, c))
    return segs


def wrap(segs: list[tuple[str, tuple]], width: int, indent: int = 0) -> list[list[tuple[str, tuple]]]:
    """Wrap coloured text at spaces (hard-break only words longer than a line)."""
    chars = [(ch, c) for s, c in segs for ch in s]
    rows: list[list[tuple[str, tuple]]] = []
    while len(chars) > width:
        cut = max((i for i in range(1, width + 1) if chars[i - 1][0] == " "), default=width)
        if cut < width * 0.4:
            cut = width
        rows.append(chars[:cut])
        rest = chars[cut:]
        chars = [(" ", TEXT)] * indent + rest
    rows.append(chars)
    out = []
    for r in rows:
        merged: list[tuple[str, tuple]] = []
        for ch, c in r:
            if merged and merged[-1][1] == c:
                merged[-1] = (merged[-1][0] + ch, c)
            else:
                merged.append((ch, c))
        out.append(merged)
    return out


# ---------------------------------------------------------------- terminal

TERM_X, TERM_Y, TERM_W, TERM_H = 150, 236, 1620, 790
FS, LH = 24, 34
COLS = 108
ROWS = 19


class Session:
    """A scripted terminal: commands typed at a fixed speed, output lines appearing in turn."""

    def __init__(self, items, cps: float = 34.0, line_gap: float = 0.085, start: float = 0.6,
                 prompt: str = "$ ", prompt_color=EMERALD):
        self.events = []  # (t_show, kind, payload)
        t = start
        for kind, payload in items:
            if kind == "pre":  # already on screen
                self.events.append((0.0, "full", (prompt, payload)))
            elif kind == "cmd":
                dur = len(payload) / cps
                self.events.append((t, "cmd", (prompt, payload, t, t + dur)))
                t += dur + 0.45
            elif kind == "out":
                for ln in payload:
                    self.events.append((t, "out", ln))
                    t += line_gap
                t += 0.35
            elif kind == "pause":
                t += payload
            elif kind == "raw":  # custom-styled line: list of (text, color)
                self.events.append((t, "raw", payload))
                t += line_gap
        self.prompt, self.prompt_color = prompt, prompt_color
        self.end = t

    def rows(self, t: float):
        rows: list[list[tuple[str, tuple]]] = []
        cursor_row = None
        for (ts, kind, p) in self.events:
            if ts > t:
                break
            if kind == "full":
                pr, text = p
                rows += wrap([(pr, self.prompt_color), *colorize(text)], COLS, 2)
            elif kind == "cmd":
                pr, text, a, b = p
                n = len(text) if t >= b else int(len(text) * (t - a) / (b - a))
                rr = wrap([(pr, self.prompt_color), (text[:n], TEXT)], COLS, 2)
                rows += rr
                if t < b + 0.45:
                    cursor_row = len(rows) - 1
            elif kind == "out":
                rows += wrap(colorize(p), COLS, 4)
            elif kind == "raw":
                rows += wrap(p, COLS, 4)
        if cursor_row is None:
            rows.append([(self.prompt, self.prompt_color)])
            cursor_row = len(rows) - 1
        return rows, cursor_row


def _shadow() -> Image.Image:
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [TERM_X, TERM_Y + 24, TERM_X + TERM_W, TERM_Y + TERM_H + 24], 26, fill=150)
    return m.filter(ImageFilter.GaussianBlur(36))


SHADOW = _shadow()


def draw_window(img: Image.Image, title: str) -> ImageDraw.ImageDraw:
    img.paste((0, 0, 0), (0, 0), SHADOW)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([TERM_X, TERM_Y, TERM_X + TERM_W, TERM_Y + TERM_H], 22, fill=TERM,
                        outline=BORDER, width=2)
    d.rounded_rectangle([TERM_X + 1, TERM_Y + 1, TERM_X + TERM_W - 1, TERM_Y + 52], 22,
                        fill=(14, 22, 38))
    d.rectangle([TERM_X + 1, TERM_Y + 30, TERM_X + TERM_W - 1, TERM_Y + 52], fill=(14, 22, 38))
    d.line([TERM_X + 1, TERM_Y + 53, TERM_X + TERM_W - 1, TERM_Y + 53], fill=BORDER, width=2)
    for i, c in enumerate([(248, 113, 113), (251, 191, 36), (52, 211, 153)]):
        cx = TERM_X + 34 + i * 30
        d.ellipse([cx - 8, TERM_Y + 19, cx + 8, TERM_Y + 35], fill=mix(c, TERM, 0.25))
    f = font(SANS, 20)
    d.text((TERM_X + (TERM_W - text_w(title, f)) / 2, TERM_Y + 13), title, font=f, fill=MUTED)
    return d


def draw_session(img: Image.Image, sess: Session, t: float, title: str,
                 highlight: tuple[str, float] | None = None) -> None:
    d = draw_window(img, title)
    rows, cur = sess.rows(t)
    top = max(0, len(rows) - ROWS)
    f = font(MONO, FS)
    x0, y0 = TERM_X + 36, TERM_Y + 78
    for i, row in enumerate(rows[top:]):
        x = x0
        y = y0 + i * LH
        for s, c in row:
            d.text((x, y), s, font=f, fill=c)
            x += text_w(s, f)
        if top + i == cur and (t * 2) % 2 < 1.25:
            d.rectangle([x + 2, y + 3, x + 15, y + LH - 5], fill=TEXT)
    if highlight:
        needle, at = highlight
        k = ease(t, at, at + 0.45)
        if k > 0:
            for i, row in enumerate(rows[top:]):
                if needle in "".join(s for s, _ in row):
                    y = y0 + i * LH
                    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    od = ImageDraw.Draw(over)
                    od.rounded_rectangle([x0 - 14, y - 4, TERM_X + TERM_W - 22, y + LH - 2], 10,
                                         fill=(52, 211, 153, int(34 * k)),
                                         outline=(52, 211, 153, int(200 * k)), width=2)
                    img.paste(over, (0, 0), over)
                    break


def draw_caption(img: Image.Image, t: float, step: str, title: str, sub: str) -> None:
    d = ImageDraw.Draw(img)
    k = ease(t, 0.0, 0.5)
    y = 70 + (1 - k) * 16
    col = mix(BG1, TEXT, k)
    fb = font(SANS_B, 26)
    pill_w = text_w(step, fb) + 36
    d.rounded_rectangle([TERM_X, y + 6, TERM_X + pill_w, y + 48], 21, fill=mix(BG1, (11, 42, 34), k),
                        outline=mix(BG1, EMERALD, k), width=2)
    d.text((TERM_X + 18, y + 10), step, font=fb, fill=mix(BG1, EMERALD, k))
    d.text((TERM_X + pill_w + 22, y - 2), title, font=font(SANS_B, 46), fill=col)
    d.text((TERM_X + pill_w + 24, y + 62), sub, font=font(SANS, 27), fill=mix(BG1, MUTED, k))


# ---------------------------------------------------------------- real output (sandbox HOME)

SERVER = ("uvx --from git+https://github.com/DyoneNunes/mcp-manage-router-skill"
          "@01c3d46c332f8095e30791b0cf70ac8748504a09 skills-router serve")

DRY_RUN_OUT = [
    "[dry-run] Runtime: ~/.ai-skills",
    "[dry-run] Clientes detectados: Claude Code, Gemini CLI, Codex CLI",
    "[dry-run] Instala a meta-skill em ~/.ai-skills/skills/skill-orchestrator/SKILL.md",
    "[dry-run] Skills importadas: 10 | duplicadas idênticas: 1 | atalhos de pacote: 0 | links quebrados: 0 | conflitos: 0",
    "[dry-run] Originais movidos para backup: 11 -> ~/.ai-skills/backups/20261126-004918",
    "[dry-run] Claude Code: MCP 'skills-router' registrado em ~/.claude.json",
    "[dry-run] Gemini CLI: MCP 'skills-router' registrado em ~/.gemini/settings.json",
    "[dry-run] Codex CLI: MCP 'skills-router' registrado em ~/.codex/config.toml",
    "[dry-run] Claude Code: ativação automática (hook UserPromptSubmit) configurada em ~/.claude/settings.json",
    "[dry-run] Gemini CLI: ativação automática (regra global) configurada em ~/.gemini/GEMINI.md",
    "[dry-run] Codex CLI: ativação automática (regra global) configurada em ~/.codex/AGENTS.md",
    "",
    "Nada foi alterado (dry-run).",
]

INIT_OUT = [
    "Runtime: ~/.ai-skills",
    "Clientes detectados: Claude Code, Gemini CLI, Codex CLI",
    "Skills importadas: 10 | duplicadas idênticas: 1 | atalhos de pacote: 0 | links quebrados: 0 | conflitos: 0",
    "Originais movidos para backup: 11 -> ~/.ai-skills/backups/20261126-004929",
    "Instala a skill incluída 'license-designer'",
    "Claude Code: .claude/skills expõe skill-orchestrator",
    "Gemini CLI: .gemini/skills expõe skill-orchestrator",
    "Codex CLI: .codex/skills expõe skill-orchestrator",
    "Claude Code: MCP 'skills-router' registrado em ~/.claude.json",
    "Gemini CLI: MCP 'skills-router' registrado em ~/.gemini/settings.json",
    "Codex CLI: MCP 'skills-router' registrado em ~/.codex/config.toml",
    "Claude Code: ativação automática (hook UserPromptSubmit) configurada em ~/.claude/settings.json",
    "Índice: 12 skills em ~/.ai-skills/index.json",
    "",
    "Pronto. Reinicie os clientes para carregar o MCP.",
]

RESTORE_OUT = [
    "[dry-run] Restaurando a partir de ~/.ai-skills/backups/20261126-004929",
    "[dry-run] MCP removido de ~/.claude.json",
    "[dry-run] MCP removido de ~/.gemini/settings.json",
    "[dry-run] MCP removido de ~/.codex/config.toml",
    "[dry-run] Ativação automática removida de ~/.claude/settings.json",
    "[dry-run] Ativação automática removida de ~/.gemini/GEMINI.md",
    "[dry-run] Ativação automática removida de ~/.codex/AGENTS.md",
    "[dry-run] Itens devolvidos ao lugar original: 11",
    "As cópias em ~/.ai-skills/skills foram mantidas.",
]

S_DRY = Session([
    ("pre", f'export SERVER="{SERVER}"'),
    ("cmd", 'skills-router init --dry-run --server-command "$SERVER"'),
    ("out", DRY_RUN_OUT),
])
S_INIT = Session([
    ("cmd", 'skills-router init --server-command "$SERVER"'),
    ("out", INIT_OUT),
])
S_RESTORE = Session([
    ("cmd", "skills-router restore --dry-run"),
    ("out", RESTORE_OUT),
])

AG = [(">", EMERALD)]
S_AGENT = Session([
    ("cmd", "Audit this repo for security issues before the release"),
    ("pause", 0.3),
    ("raw", [("  hook  ", DIM), ("[skills-router]", AMBER),
             (" ... your FIRST action must be search_installed_skills", MUTED)]),
    ("pause", 0.7),
    ("raw", []),
    ("raw", [("● ", CYAN), ("search_installed_skills", CYAN), ('(query="security audit owasp", limit=3)', TEXT)]),
    ("pause", 0.5),
    ("raw", [("  └ ", DIM), ('{"total_indexed": 12, "results": [{"name": "security-audit", "score": 0.714,', MUTED)]),
    ("raw", [("    ", DIM), ('"description": "Audit a codebase for OWASP Top 10 vulnerabilities, secrets and ', MUTED)]),
    ("raw", [("    ", DIM), ('unsafe dependencies.", "path": "~/.ai-skills/skills/security-audit/SKILL.md"}]}', MUTED)]),
    ("pause", 0.9),
    ("raw", []),
    ("raw", [("● ", CYAN), ("get_skill_content", CYAN), ('(skill_name="security-audit")', TEXT)]),
    ("pause", 0.5),
    ("raw", [("  └ ", DIM), ('{"name": "security-audit", "path": "~/.ai-skills/skills/security-audit/SKILL.md",', MUTED)]),
    ("raw", [("    ", DIM), ('"resources": [], "content": "---\\nname: security-audit\\n...", "truncated": false}', MUTED)]),
    ("pause", 0.8),
    ("raw", []),
    ("raw", [("● ", TEXT), ("Loaded 1 of 12 skills. Following security-audit: OWASP Top 10, secrets, dependencies.", TEXT)]),
], cps=30, line_gap=0.12, prompt="> ", prompt_color=EMERALD)


# ---------------------------------------------------------------- scenes

def scene_title(img: Image.Image, t: float) -> None:
    d = ImageDraw.Draw(img)
    k = ease(t, 0.1, 0.9)
    size = 300
    lg = logo(size)
    a = lg.copy()
    a.putalpha(a.getchannel("A").point(lambda v: int(v * k)))
    img.paste(a, ((W - size) // 2, int(250 - (1 - k) * 30)), a)
    k2 = ease(t, 0.6, 1.3)
    draw_center(d, 600, "Skills Router MCP", font(SANS_B, 84), mix(BG1, TEXT, k2))
    k3 = ease(t, 1.0, 1.7)
    draw_center(d, 718, "Hundreds of skills installed. Only the one you need in context.",
                font(SANS, 36), mix(BG1, MUTED, k3))


def scene_problem(img: Image.Image, t: float) -> None:
    d = ImageDraw.Draw(img)
    k = ease(t, 0.0, 0.5)
    draw_center(d, 150, "Every session, your agent reads every skill's description first.",
                font(SANS_B, 46), mix(BG1, TEXT, k))
    draw_center(d, 222, "Measured on a real library of 319 skills, before any task runs:",
                font(SANS, 30), mix(BG1, MUTED, k))
    x0, bw = 260, 1400
    fb, fn = font(SANS_B, 32), font(SANS_B, 40)
    # native
    k1 = ease(t, 0.7, 2.2)
    d.text((x0, 360), "Native skill catalog", font=fb, fill=mix(BG1, TEXT, ease(t, 0.5, 0.9)))
    d.rounded_rectangle([x0, 420, x0 + bw, 500], 16, fill=(18, 28, 46))
    if k1 > 0:
        d.rounded_rectangle([x0, 420, x0 + max(32, bw * k1), 500], 16, fill=(248, 113, 113))
    n1 = f"~{int(33800 * k1):,} tokens"
    d.text((x0 + bw - text_w(n1, fn), 352), n1, font=fn, fill=mix(BG1, (248, 113, 113), ease(t, 0.7, 1.0)))
    # router
    k2 = ease(t, 2.5, 3.3)
    d.text((x0, 580), "With skills-router", font=fb, fill=mix(BG1, TEXT, ease(t, 2.3, 2.7)))
    d.rounded_rectangle([x0, 640, x0 + bw, 720], 16, fill=(18, 28, 46))
    if k2 > 0:
        d.rounded_rectangle([x0, 640, x0 + max(32, bw * 530 / 33800 * k2) + 22, 720], 16, fill=EMERALD)
    n2 = f"~{int(530 * k2):,} tokens"
    d.text((x0 + bw - text_w(n2, fn), 572), n2, font=fn, fill=mix(BG1, EMERALD, ease(t, 2.5, 2.8)))
    k3 = ease(t, 3.6, 4.2)
    draw_center(d, 790, "98% less context", font(SANS_B, 78), mix(BG1, EMERALD, k3))
    draw_center(d, 900, "~4 characters per token · method and caveats in reference/architecture.md",
                font(SANS, 24), mix(BG1, DIM, k3))


def term_scene(sess: Session, step: str, title: str, sub: str, wtitle: str, highlight=None):
    def f(img: Image.Image, t: float) -> None:
        draw_caption(img, t, step, title, sub)
        draw_session(img, sess, t, wtitle, highlight)
    return f


def scene_end(img: Image.Image, t: float) -> None:
    d = ImageDraw.Draw(img)
    k = ease(t, 0.0, 0.6)
    size = 220
    a = logo(size).copy()
    a.putalpha(a.getchannel("A").point(lambda v: int(v * k)))
    img.paste(a, ((W - size) // 2, 170), a)
    draw_center(d, 430, "Skills Router MCP", font(SANS_B, 76), mix(BG1, TEXT, k))
    k2 = ease(t, 0.4, 1.0)
    draw_center(d, 540, "Claude Code · Gemini CLI · Antigravity · Codex CLI · opencode · Cursor",
                font(SANS, 34), mix(BG1, MUTED, k2))
    k3 = ease(t, 0.8, 1.4)
    items = ["Dry-run first", "Backups + one-command restore", "New skills need your approval"]
    fb = font(SANS_B, 30)
    gap = 28
    widths = [text_w(s, fb) + 56 for s in items]
    x = (W - sum(widths) - gap * (len(items) - 1)) / 2
    for s, w in zip(items, widths):
        d.rounded_rectangle([x, 640, x + w, 704], 32, fill=mix(BG1, (11, 42, 34), k3),
                            outline=mix(BG1, EMERALD, k3), width=2)
        d.text((x + 28, 652), s, font=fb, fill=mix(BG1, EMERALD, k3))
        x += w + gap
    k4 = ease(t, 1.3, 1.9)
    draw_center(d, 800, "Get the skill on agensi.io", font(SANS_B, 40), mix(BG1, CYAN, k4))


SCENES = [
    (4.0, scene_title),
    (6.0, scene_problem),
    (S_DRY.end + 2.6, term_scene(S_DRY, "1", "Preview every change",
                                 "init --dry-run lists what it would import, register and back up. Nothing is written.",
                                 "bash — ~", ("Nada foi alterado", S_DRY.end + 0.1))),
    (S_INIT.end + 2.6, term_scene(S_INIT, "2", "Install once, for every agent",
                                  "Imports your skills into one library, registers the MCP server and the hook, with backups.",
                                  "bash — ~", ("Skills importadas", S_INIT.end + 0.1))),
    (S_AGENT.end + 2.8, term_scene(S_AGENT, "3", "Your agent finds the skill itself",
                                   "The hook makes it search first, then it loads only the match.",
                                   "agent — ~/project", ("Loaded 1 of 12", S_AGENT.end + 0.1))),
    (S_RESTORE.end + 2.4, term_scene(S_RESTORE, "4", "Undo everything in one command",
                                     "restore puts skills and configs back from the backup.",
                                     "bash — ~", ("Itens devolvidos", S_RESTORE.end + 0.1))),
    (5.0, scene_end),
]

FADE = 0.35


def frame(t: float) -> Image.Image:
    acc = 0.0
    for dur, fn in SCENES:
        if t < acc + dur or fn is SCENES[-1][1]:
            lt = t - acc
            img = BACKGROUND.copy()
            fn(img, lt)
            k = min(ease(lt, 0, FADE), 1 - ease(lt, dur - FADE, dur))
            if fn is SCENES[0][1]:
                k = 1 - ease(lt, dur - FADE, dur)
            if fn is SCENES[-1][1]:
                k = ease(lt, 0, FADE)
            if k < 1:
                img = Image.blend(BACKGROUND, img, k)
            return img
        acc += dur
    raise AssertionError


def total() -> float:
    return sum(d for d, _ in SCENES)


def main() -> None:
    out = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "--stills":
        for s in sys.argv[3:]:
            frame(float(s)).save(f"{out}-{s}.png")
        return
    n = int(total() * FPS)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "slow",
         "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
        stdin=subprocess.PIPE)
    for i in range(n):
        ff.stdin.write(frame(i / FPS).tobytes())
    ff.stdin.close()
    ff.wait()
    print(f"{out}: {n} frames, {total():.1f}s")


if __name__ == "__main__":
    main()
