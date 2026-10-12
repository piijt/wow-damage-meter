"""Details!-style live meter for the WoW combat log.  Run:  python app.py   (--live skips replaying the log)

Meter window: drag the header, resize from the bottom-right corner, mouse wheel scrolls.
Click a bar for the full breakdown, hover for a quick card.
"""
import math, os, sys, time, tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
from datetime import datetime
from wowlog import State, Segment, start_tail, CLASS_COLOR
import store

# ----------------------------------------------------------------------------- look
BG, PANEL, PANEL2, LINE = "#0d0e12", "#15171d", "#1c1f27", "#262a34"
FG, DIM, FAINT = "#eef0f4", "#8a91a1", "#525868"
ACCENT, ACCENT2 = "#8f7cff", "#4fd1ff"
GOOD, BAD, CRIT = "#3ddc97", "#ff5c6c", "#ffc94d"
ROW, GAP = 22, 2
MODES = [("Damage Done", "DAMAGE"), ("DPS", "DPS"), ("Healing Done", "HEALING"), ("HPS", "HPS"),
         ("Damage Taken", "TAKEN"), ("Deaths", "DEATHS"), ("Interrupts", "KICKS"), ("Dispels", "DISPELS")]
SHORT = dict(MODES)
F = {}


def init_fonts():
    F["name"] = tkfont.Font(family="Segoe UI Semibold", size=9)
    F["num"] = tkfont.Font(family="Bahnschrift SemiBold", size=10)
    F["small"] = tkfont.Font(family="Bahnschrift", size=8)
    F["mid"] = tkfont.Font(family="Bahnschrift SemiBold", size=13)
    F["big"] = tkfont.Font(family="Bahnschrift SemiBold", size=18)
    F["huge"] = tkfont.Font(family="Bahnschrift SemiBold", size=22)
    F["title"] = tkfont.Font(family="Bahnschrift SemiBold", size=10)
    F["label"] = tkfont.Font(family="Segoe UI", size=8)
    F["ui"] = tkfont.Font(family="Segoe UI", size=9)
    F["ui_b"] = tkfont.Font(family="Segoe UI Semibold", size=10)


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    a, b = rgb(a), rgb(b)
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(a[i] + (b[i] - a[i]) * t))) for i in range(3))


def shade(h, f):
    return mix(h, "#000000", 1 - f) if f < 1 else mix(h, "#ffffff", f - 1)


def hgrad(c, x0, y0, x1, y1, c0, c1, steps=24):
    w = (x1 - x0) / steps
    for i in range(steps):
        c.create_rectangle(x0 + i * w, y0, x0 + (i + 1) * w + 1, y1, fill=mix(c0, c1, i / max(steps - 1, 1)), width=0)


def vgrad(c, x0, y0, x1, y1, c0, c1, steps=8):
    h = (y1 - y0) / steps
    for i in range(steps):
        c.create_rectangle(x0, y0 + i * h, x1, y0 + (i + 1) * h + 1, fill=mix(c0, c1, i / max(steps - 1, 1)), width=0)


def pill(c, x0, y0, x1, y1, fill, outline="", tags=()):
    r = (y1 - y0) / 2
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, fill=fill, outline=outline, tags=tags)


def stext(c, x, y, text, font, fill, anchor="w", tags=()):
    c.create_text(x + 1, y + 1, text=text, font=font, fill="#000000", anchor=anchor, tags=tags)
    return c.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor, tags=tags)


def fit(text, font, width):
    if font.measure(text) <= width:
        return text
    while len(text) > 2 and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def fmt(n):
    if n is None:
        return "-"
    n = float(n)
    if abs(n) >= 1e9: return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6: return f"{n / 1e6:.2f}M"
    if abs(n) >= 1e3: return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def mmss(sec):
    m, s = divmod(max(int(sec), 0), 60)
    return f"{m}:{s:02d}"


def tiles(c, items, x0, y0, x1, h, gap=6):
    """Row of stat cards: items = (label, value, sub, color)."""
    w = (x1 - x0 - gap * (len(items) - 1)) / max(len(items), 1)
    for i, (label, value, sub, color) in enumerate(items):
        x = x0 + i * (w + gap)
        c.create_rectangle(x, y0, x + w, y0 + h, fill=PANEL2, width=0)
        c.create_rectangle(x, y0, x + w, y0 + 2, fill=color, width=0)
        c.create_text(x + 10, y0 + 13, text=label, anchor="w", fill=FAINT, font=F["small"])
        c.create_text(x + 10, y0 + 31, text=value, anchor="w", fill=FG, font=F["mid"])
        if sub:
            c.create_text(x + 10, y0 + h - 9, text=fit(sub, F["small"], w - 16), anchor="w", fill=DIM, font=F["small"])


def style():
    st = ttk.Style(); st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, font=F["ui"], fieldbackground=PANEL, bordercolor=BG,
                 lightcolor=BG, darkcolor=BG)
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24, borderwidth=0)
    st.configure("Treeview.Heading", background=PANEL2, foreground=DIM, relief="flat", font=F["small"], padding=(6, 5))
    st.map("Treeview.Heading", background=[("active", LINE)])
    st.map("Treeview", background=[("selected", mix(ACCENT, PANEL, 0.55))], foreground=[("selected", "#ffffff")])
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=DIM, padding=(16, 7), font=F["title"], borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", FG)])
    st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL, arrowcolor=DIM, borderwidth=0,
                 gripcount=0, width=10)
    st.map("Vertical.TScrollbar", background=[("active", LINE)])


def make_tree(master, cols, height=None):
    fr = tk.Frame(master, bg=BG)
    t = ttk.Treeview(fr, columns=[c[0] for c in cols], show="headings", selectmode="browse",
                     **({"height": height} if height else {}))
    for c in cols:
        anchor = c[2] if len(c) > 2 else "e"
        t.heading(c[0], text=c[0].upper(), anchor=anchor)
        t.column(c[0], width=c[1], anchor=anchor, stretch=anchor == "w")
    sb = ttk.Scrollbar(fr, orient="vertical", command=t.yview); t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
    for tag, col in (("crit", CRIT), ("bad", BAD), ("good", GOOD), ("dim", DIM), ("cast", "#b9aeff")):
        t.tag_configure(tag, foreground=col)
    return t


def button(master, text, cmd, color=ACCENT):
    b = tk.Label(master, text=text, bg=mix(color, PANEL, 0.7), fg=FG, font=F["title"], padx=12, pady=4,
                 cursor="hand2")
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>", lambda e: b.config(bg=mix(color, PANEL, 0.45)))
    b.bind("<Leave>", lambda e: b.config(bg=mix(color, PANEL, 0.7)))
    return b


def toplevel(root, title, geo):
    w = tk.Toplevel(root); w.configure(bg=BG); w.geometry(geo); w.attributes("-topmost", True); w.title(title)
    return w


# =========================================================================== meter window
class Meter:
    def __init__(self, root, state, history):
        self.root, self.s, self.store = root, state, history
        self.mode, self.view = "Damage Done", "live"
        self.scroll, self.rows, self.tip, self.tip_key, self.hover_i = 0, [], None, None, None
        self.breakdowns, self.disp, self.dirty, self.live = {}, {}, True, False
        self.empty_msg = ""
        root.overrideredirect(True)
        root.attributes("-topmost", True); root.attributes("-alpha", 0.95)
        root.geometry("360x330+80+220"); root.configure(bg=LINE)            # 1px border
        init_fonts(); style()
        inner = tk.Frame(root, bg=BG); inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.hdr = tk.Canvas(inner, height=30, bg=PANEL, highlightthickness=0); self.hdr.pack(fill="x")
        self.hdr.bind("<ButtonPress-1>", self.drag_start); self.hdr.bind("<B1-Motion>", self.drag)
        for tag, cmd in (("seg", self.seg_menu), ("mode", self.mode_menu), ("reset", self.reset),
                         ("close", root.destroy)):
            self.hdr.tag_bind(tag, "<ButtonRelease-1>", lambda e, cmd=cmd: cmd())
            self.hdr.tag_bind(tag, "<Enter>", lambda e: self.hdr.config(cursor="hand2"))
            self.hdr.tag_bind(tag, "<Leave>", lambda e: self.hdr.config(cursor=""))

        self.livec = tk.Canvas(inner, height=54, bg=PANEL, highlightthickness=0); self.livec.pack(fill="x")
        self.livec.bind("<ButtonPress-1>", self.drag_start); self.livec.bind("<B1-Motion>", self.drag)

        self.foot = tk.Canvas(inner, height=18, bg=BG, highlightthickness=0)
        self.foot.pack(side="bottom", fill="x")             # packed before the bars so it can never be squeezed out
        self.c = tk.Canvas(inner, height=60, bg=BG, highlightthickness=0)
        self.c.pack(fill="both", expand=True, pady=(4, 0))
        self.c.bind("<Button-1>", self.click); self.c.bind("<Motion>", self.hover)
        self.c.bind("<Leave>", lambda e: self.unhover())
        self.c.bind("<MouseWheel>", self.wheel)
        self.c.bind("<Configure>", lambda e: setattr(self, "dirty", True))

        # bottom-right corner resizes, the rest of the footer moves the window
        self.foot.bind("<ButtonPress-1>", self.foot_press)
        self.foot.bind("<B1-Motion>", lambda e: self.resize(e) if self._resizing else self.drag(e))
        self.foot.bind("<Motion>", lambda e: self.foot.config(
            cursor="size_nw_se" if e.x > self.foot.winfo_width() - 40 else "fleur"))
        self._resizing = False
        self.data_tick(); self.frame()

    # ---------------------------------------------------------------- window chrome
    def drag_start(self, e):
        self._dx, self._dy = e.x_root, e.y_root
        self._geo = (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())

    def foot_press(self, e):
        self._resizing = e.x > self.foot.winfo_width() - 40
        self.drag_start(e)

    def drag(self, e):
        x, y, *_ = self._geo
        self.root.geometry(f"+{x + e.x_root - self._dx}+{y + e.y_root - self._dy}")

    def resize(self, e):
        x, y, w, h = self._geo
        self.root.geometry(f"{max(260, w + e.x_root - self._dx)}x{max(160, h + e.y_root - self._dy)}")

    def wheel(self, e):
        self.scroll = max(0, self.scroll + (-1 if e.delta > 0 else 1)); self.dirty = True

    # ---------------------------------------------------------------- menus
    def menu(self):
        return tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=FG, activebackground=mix(ACCENT, PANEL, 0.5),
                       activeforeground="#ffffff", font=F["ui"], bd=0, relief="flat")

    def popup(self, m):
        m.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def seg_menu(self):
        m = self.menu()
        m.add_command(label="  ◷  History…  (saved fights)", command=lambda: History(self))
        m.add_command(label="  ⚔  Arena record…  (2v2 · 3v3 · Solo Shuffle)", command=lambda: Arena(self))
        m.add_separator()
        m.add_command(label="  ●  Current (follow live)", command=lambda: self.set_view("live"))
        m.add_command(label="  Σ  Overall", command=lambda: self.set_view("overall"))
        m.add_separator()
        with self.s.lock:
            segs = list(reversed(self.s.segments))
            labels = [g.label() for g in segs]
        for g, lab in zip(segs, labels):
            m.add_command(label="      " + lab, command=lambda g=g: self.set_view(g))
        self.popup(m)

    def mode_menu(self):
        m = self.menu()
        for md, _ in MODES:
            m.add_command(label=("  ●  " if md == self.mode else "      ") + md, command=lambda md=md: self.set_mode(md))
        self.popup(m)

    def set_view(self, v):
        self.view, self.scroll = v, 0; self.update_data()

    def set_mode(self, md):
        self.mode, self.scroll = md, 0; self.disp.clear(); self.update_data()

    def reset(self):
        with self.s.lock:
            self.s.reset()
        self.view, self.scroll = "live", 0
        self.disp.clear(); self.update_data()

    def segment(self):
        s = self.s
        if self.view == "overall":
            return s.overall
        if self.view == "live":
            return s.segments[-1] if s.segments else None
        if isinstance(self.view, Segment) and (getattr(self.view, "history_id", None) or self.view in s.segments):
            return self.view
        return s.segments[-1] if s.segments else None

    def is_live(self, seg):
        return seg is not None and seg is self.s.cur

    # ---------------------------------------------------------------- data (4x per second)
    def data_tick(self):
        try:
            self.update_data()
        finally:
            self.root.after(250, self.data_tick)

    def update_data(self):
        s = self.s
        with s.lock:
            seg = self.segment()
            self.live = self.is_live(seg)
            if seg is None:
                seg_text = "No combat yet"
            elif seg is s.overall:
                seg_text = "Overall"
            elif getattr(seg, "history_id", None):
                seg_text = "◷ " + seg.label()
            else:
                seg_text = seg.label().split("  ", 1)[1].rsplit("  (", 1)[0]
            self.rows = self.build_rows(seg) if seg else []
            self.empty_msg = "type /combatlog in game, then fight" if seg is None else f"no {self.mode.lower()} yet"
            self.draw_header(seg_text)
            self.draw_live(seg)
            prog = s.progress
            idle = time.time() - s.last_read
            foot = (f"loading log  {100 * prog:.0f}%" if prog < 0.999 else
                    f"⚠ log idle {int(idle // 60)}m — type /combatlog in game" if idle > 120 else
                    f"{(s.my_name or 'waiting for you').split('-')[0]}   ·   {s.lines:,} lines   ·   {self.store.saved} saved")
        self.draw_foot(foot)
        self.dirty = True

    def build_rows(self, seg):
        md, s, dur = self.mode, self.s, seg.duration
        live = self.is_live(seg)
        row = lambda key, left, main, sub, frac, color, payload, hostile: dict(
            key=key, left=left, main=main, sub=sub, frac=frac, color=color, payload=payload, hostile=hostile,
            me=key == s.me)
        if md == "Deaths":
            out = []
            for d in reversed(seg.deaths):
                a = seg.actors.get(d["key"])
                out.append(row(("death", id(d)), f"{d['name']}  ☠  {d['killer']}", mmss(d["t"] - seg.t0), "",
                               1.0, s.color(a) if a else "#7f8c99", d, d["hostile"]))
            return out
        actors = list(seg.actors.values())
        if md in ("Interrupts", "Dispels"):
            attr = "interrupts" if md == "Interrupts" else "dispels"
            vals = [(a, len(getattr(a, attr))) for a in actors if getattr(a, attr)]
            top = max([v for _, v in vals] + [1])
            return [row(a.key, a.name, str(v), "", v / top, s.color(a), a, a.hostile)
                    for a, v in sorted(vals, key=lambda x: -x[1])]
        attr = {"Damage Done": "dmg_total", "DPS": "dmg_total", "Healing Done": "heal_total",
                "HPS": "heal_total", "Damage Taken": "taken_total"}[md]
        vals = [(a, getattr(a, attr)) for a in actors if getattr(a, attr) > 0]
        total = sum(v for _, v in vals) or 1
        top = max([v for _, v in vals] + [1])
        out = []
        for a, v in sorted(vals, key=lambda x: -x[1]):
            if md in ("DPS", "HPS"):
                main = fmt(v / dur)
                sub = f"now {fmt(a.rate('dmg' if md == 'DPS' else 'heal', s.log_now))}" if live else fmt(v)
            else:
                main, sub = fmt(v), f"{fmt(v / dur)}  {100 * v / total:.0f}%"
            out.append(row(a.key, a.name, main, sub, v / top, s.color(a), a, a.hostile))
        return out

    # ---------------------------------------------------------------- drawing
    def draw_header(self, seg_text):
        c = self.hdr; c.delete("all"); w = c.winfo_width()
        vgrad(c, 0, 0, w, 30, "#1e2129", "#121419", steps=6)
        hgrad(c, 0, 28, w, 30, ACCENT, ACCENT2, steps=28)
        x = w - 14
        for icon, tag in (("✕", "close"), ("⟲", "reset")):
            c.create_rectangle(x - 10, 4, x + 10, 26, fill="", outline="", tags=(tag,))   # bigger hit area
            c.create_text(x, 14, text=icon, fill=DIM, font=F["ui_b"], tags=(tag,))
            x -= 24
        mode = SHORT[self.mode] + "  ▾"
        mw = F["small"].measure(mode) + 20
        pill(c, x - mw + 10, 7, x + 10, 22, PANEL2, outline=mix(ACCENT, PANEL, 0.4), tags=("mode",))
        c.create_text(x - mw / 2 + 10, 14.5, text=mode, fill=FG, font=F["small"], tags=("mode",))
        tx = 10
        if self.live:
            c.create_oval(9, 10, 17, 18, fill=BAD, width=0, tags=("seg",)); tx = 23
        text = fit(seg_text, F["title"], x - mw - tx - 14) + "  ▾"
        c.create_rectangle(0, 2, x - mw, 27, fill="", outline="", tags=("seg",))
        c.create_text(tx, 14.5, text=text, anchor="w", fill=FG, font=F["title"], tags=("seg",))

    def draw_live(self, seg):
        c, s = self.livec, self.s
        c.delete("all"); w, h = c.winfo_width(), 54
        vgrad(c, 0, 0, w, h, PANEL, "#101217", steps=6)
        if seg is None:
            c.create_text(w / 2, h / 2, text="waiting for combat", fill=DIM, font=F["title"]); return
        live = self.live
        elapsed = time.time() - seg.start if live and seg.start else seg.duration
        tag = ("IN COMBAT" if live else "HISTORY" if getattr(seg, "history_id", None)
               else "OVERALL" if seg is s.overall else "LAST FIGHT")
        c.create_text(12, 22, text=mmss(elapsed), anchor="w", fill=FG if live else DIM, font=F["big"])
        c.create_text(13, 43, text=tag, anchor="w", fill=BAD if live else FAINT, font=F["small"])
        me = seg.actors.get(s.me)
        if me is None:
            return
        heal = self.mode in ("Healing Done", "HPS")
        if live and s.delay is not None and s.delay >= 1.5:     # only shown when WoW's log is lagging
            c.create_text(w - 12, 6, text=f"LOG +{s.delay:.1f}s", anchor="ne", fill=CRIT, font=F["small"])
        x0 = 12 + F["big"].measure("00:00") + 16
        x1 = w - 12
        if x1 - x0 > 40:
            self.sparkline(c, me.hbuckets if heal else me.buckets, seg, x0, 10, x1, 46, live)

    def sparkline(self, c, buckets, seg, x0, y0, x1, y1, live):
        end, n = int(self.s.log_now if live else seg.last), 30
        vals = [buckets.get(end - n + 1 + i, 0) for i in range(n)]
        sm = [sum(vals[max(0, i - 2):i + 1]) / len(vals[max(0, i - 2):i + 1]) for i in range(n)]
        top = max(sm + [1])
        pts = [(x0 + (x1 - x0) * i / (n - 1), y1 - (y1 - y0) * v / top) for i, v in enumerate(sm)]
        c.create_line(x0, y1, x1, y1, fill=LINE)
        c.create_polygon([x0, y1] + [p for xy in pts for p in xy] + [x1, y1], fill=mix(ACCENT, PANEL, 0.72), outline="")
        c.create_line(*[p for xy in pts for p in xy], fill=ACCENT, width=2, smooth=True)
        c.create_oval(pts[-1][0] - 3, pts[-1][1] - 3, pts[-1][0] + 3, pts[-1][1] + 3, fill=ACCENT2, width=0)
        c.create_text(x0, y0 - 2, text="30s", anchor="nw", fill=FAINT, font=F["small"])

    def draw_foot(self, text):
        c = self.foot; c.delete("all"); w = c.winfo_width()
        c.create_text(8, 9, text=text, anchor="w", fill=CRIT if text.startswith("⚠") else FAINT, font=F["small"])
        for i in range(3):                                   # dotted resize grip
            for j in range(i + 1):
                x, y = w - 5 - (i - j) * 4, 15 - j * 4
                c.create_rectangle(x - 1, y - 1, x + 1, y + 1, fill=DIM, width=0)

    def frame(self):
        """20 fps: ease bars toward their target width."""
        try:
            moved = False
            for r in self.rows:
                cur, tgt = self.disp.get(r["key"], 0.0), r["frac"]
                if abs(tgt - cur) > 0.002:
                    cur += (tgt - cur) * 0.28; moved = True
                else:
                    cur = tgt
                self.disp[r["key"]] = cur
            if moved or self.dirty:
                self.draw_bars(); self.dirty = False
        finally:
            self.root.after(50, self.frame)

    def draw_bars(self):
        c = self.c; c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        rows, step = self.rows, ROW + GAP
        visible = max(h // step, 1)
        self.scroll = min(self.scroll, max(len(rows) - visible, 0))
        if not rows:
            c.create_text(w / 2, h / 2 - 8, text=self.empty_msg, fill=DIM, font=F["ui"]); return
        for n, r in enumerate(rows[self.scroll:self.scroll + visible]):
            i = self.scroll + n
            y0, y1 = n * step + 1, n * step + 1 + ROW
            mid = (y0 + y1) / 2
            hov = i == self.hover_i
            c.create_rectangle(6, y0, w - 6, y1, fill=PANEL2 if hov else PANEL, width=0)
            side = BAD if r["hostile"] else GOOD                       # rank badge = side marker
            bx = 6 + ROW + 4
            c.create_rectangle(6, y0, 6 + ROW + 4, y1, fill=mix(side, PANEL, 0.78), width=0)
            c.create_rectangle(6, y0, 8, y1, fill=side, width=0)
            c.create_text(6 + (ROW + 4) / 2 + 1, mid, text=str(i + 1), fill=side, font=F["small"])
            col = shade(r["color"], 1.1) if hov else r["color"]
            bw = (w - 6 - bx) * self.disp.get(r["key"], 0)
            if bw > 1:
                c.create_rectangle(bx, y0, bx + bw, mid, fill=shade(col, 0.92), width=0)
                c.create_rectangle(bx, mid, bx + bw, y1, fill=shade(col, 0.68), width=0)
                c.create_line(bx, y0, bx + bw, y0, fill=shade(col, 1.35))
                c.create_rectangle(max(bx, bx + bw - 2), y0, bx + bw, y1, fill=shade(col, 1.3), width=0)
            if r["me"]:
                c.create_rectangle(6, y0, w - 7, y1 - 1, outline=shade(r["color"], 1.45), width=1)
            rx = w - 12
            if r["sub"]:
                stext(c, rx, mid, r["sub"], F["small"], "#d3d6de", "e")
                rx -= F["small"].measure(r["sub"]) + 8
            stext(c, rx, mid, r["main"], F["num"], "#ffffff", "e")
            room = rx - F["num"].measure(r["main"]) - bx - 18
            stext(c, bx + 7, mid, fit(r["left"], F["name"], room), F["name"], "#ffd3d8" if r["hostile"] else FG, "w")

    def row_at(self, y):
        i = self.scroll + int((y - 1) // (ROW + GAP))
        return (i, self.rows[i]) if 0 <= i < len(self.rows) else (None, None)

    # ---------------------------------------------------------------- interaction
    def click(self, e):
        _, r = self.row_at(e.y)
        if not r:
            return
        self.hide_tip()
        if self.mode == "Deaths":
            DeathRecap(self.root, self.s, r["payload"]); return
        win = self.breakdowns.get(r["key"])
        if win and win.alive():
            win.focus(self.mode, self.view)
        else:
            self.breakdowns[r["key"]] = Breakdown(self, r["key"], self.mode, self.view)

    def unhover(self):
        self.hover_i = None; self.dirty = True; self.hide_tip()

    def hover(self, e):
        i, r = self.row_at(e.y)
        if i != self.hover_i:
            self.hover_i = i; self.dirty = True
        if not r or self.mode == "Deaths":
            self.hide_tip(); return
        if r["key"] != self.tip_key:
            self.hide_tip(); self.tip_key = r["key"]
            self.tip = self.make_tip(r["payload"])
        self.tip.geometry(f"+{e.x_root + 18}+{e.y_root + 14}")

    def make_tip(self, a):
        with self.s.lock:
            col, spec, lines = self.s.color(a), self.s.spec(a), self.tip_lines(a)
        W, H = 270, 52 + len(lines) * 20 + 20
        tip = tk.Toplevel(self.root); tip.overrideredirect(True); tip.attributes("-topmost", True)
        tip.attributes("-alpha", 0.96)
        c = tk.Canvas(tip, width=W, height=H, bg=PANEL, highlightthickness=1, highlightbackground=mix(col, PANEL, 0.4))
        c.pack()
        hgrad(c, 0, 0, W, 42, mix(col, PANEL, 0.55), PANEL, steps=20)
        c.create_text(12, 15, text=a.name, anchor="w", fill=shade(col, 1.2), font=F["title"])
        c.create_text(12, 31, text=f"{spec}  ·  {'enemy' if a.hostile else 'friendly'}", anchor="w", fill=DIM,
                      font=F["label"])
        y = 50
        for name, val, frac in lines:
            c.create_rectangle(12, y + 14, W - 12, y + 16, fill=PANEL2, width=0)
            c.create_rectangle(12, y + 14, 12 + (W - 24) * frac, y + 16, fill=col, width=0)
            c.create_text(12, y + 6, text=fit(name, F["label"], W - 100), anchor="w", fill=FG, font=F["label"])
            c.create_text(W - 12, y + 6, text=val, anchor="e", fill=DIM, font=F["small"])
            y += 20
        c.create_text(12, H - 10, text="click for full breakdown", anchor="w", fill=FAINT, font=F["small"])
        return tip

    def tip_lines(self, a):
        if self.mode in ("Interrupts", "Dispels"):
            lst = a.interrupts if self.mode == "Interrupts" else a.dispels
            return [(f"{sp} → {tgt}", what, 1.0) for _, sp, tgt, what in lst[-8:]]
        book = {"Healing Done": a.heal, "HPS": a.heal, "Damage Taken": a.taken}.get(self.mode, a.dmg)
        total = sum(sp.total for sp in book.values()) or 1
        top = sorted(book.values(), key=lambda sp: -sp.total)[:8]
        best = top[0].total if top else 1
        return [(sp.name, f"{fmt(sp.total)}  {100 * sp.total / total:.0f}%", sp.total / best) for sp in top]

    def hide_tip(self):
        if self.tip:
            self.tip.destroy()
        self.tip, self.tip_key = None, None


# =========================================================================== breakdown window
class Breakdown:
    TABS = [("DAMAGE DONE", "dmg"), ("HEALING DONE", "heal"), ("DAMAGE TAKEN", "taken"), ("FRIENDLY FIRE", "ff")]

    def __init__(self, meter, key, mode, view):
        self.m, self.s, self.key, self.view = meter, meter.s, key, view
        self.w = toplevel(meter.root, "Breakdown", "1120x720")
        self.banner = tk.Canvas(self.w, height=78, bg=BG, highlightthickness=0); self.banner.pack(fill="x")
        self.chart = tk.Canvas(self.w, height=92, bg=BG, highlightthickness=0)
        self.chart.pack(fill="x", padx=14, pady=(8, 2))
        self.nb = ttk.Notebook(self.w); self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 14))
        self.panels = {}
        for title, book in self.TABS:
            self.panels[book] = SpellPanel(self.nb, self, book); self.nb.add(self.panels[book].frame, text=title)
        tab = tk.Frame(self.nb, bg=BG); self.nb.add(tab, text="CASTS")
        self.t_casts = make_tree(tab, [("Time", 70), ("Spell", 240, "w"), ("Target", 200, "w"), ("Gap", 80),
                                       ("Resource", 170)])
        self.t_casts.master.pack(fill="both", expand=True, pady=(8, 0))
        tab = tk.Frame(self.nb, bg=BG); self.nb.add(tab, text="INTERRUPTS & DISPELS")
        self.t_kicks = make_tree(tab, [("Time", 70), ("Type", 90, "w"), ("Used", 220, "w"), ("Target", 200, "w"),
                                       ("Stopped / removed", 240, "w")])
        self.t_kicks.master.pack(fill="both", expand=True, pady=(8, 0))
        self.n_casts, self.n_kicks, self.seg_seen = 0, 0, None
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.tick(once=True))
        self.focus(mode, view)
        self.tick()

    def alive(self):
        return self.w.winfo_exists()

    def focus(self, mode, view):
        self.view = view
        idx = {"Healing Done": 1, "HPS": 1, "Damage Taken": 2, "Interrupts": 5, "Dispels": 5}.get(mode, 0)
        self.nb.select(idx); self.w.lift()

    def segment(self):
        s = self.s
        if self.view == "overall":
            return s.overall
        if isinstance(self.view, Segment) and (getattr(self.view, "history_id", None) or self.view in s.segments):
            return self.view
        return s.segments[-1] if s.segments else None

    def tick(self, once=False):
        if not self.alive():
            return
        try:
            with self.s.lock:
                self.refresh()
        finally:
            if not once:
                self.w.after(600, self.tick)

    def current_book(self):
        try:
            idx = self.nb.index("current")
        except tk.TclError:
            return None
        return self.TABS[idx][1] if idx < len(self.TABS) else None

    def refresh(self):
        seg = self.segment()
        a = seg.actors.get(self.key) if seg else None
        if seg is not self.seg_seen:
            self.seg_seen = seg
            self.n_casts = self.n_kicks = 0
            self.t_casts.delete(*self.t_casts.get_children()); self.t_kicks.delete(*self.t_kicks.get_children())
            for p in self.panels.values():
                p.reset()
        if a is None:
            self.banner.delete("all")
            self.banner.create_text(16, 39, text="not in this segment", anchor="w", fill=DIM, font=F["big"]); return
        self.w.title(f"{a.name} — breakdown")
        self.draw_banner(seg, a)
        book = self.current_book()
        for b, p in self.panels.items():
            if b == book:
                p.refresh(seg, a)
        self.draw_chart(seg, a, book)
        prev = a.casts[self.n_casts - 1][0] if self.n_casts else None
        for t, spell, tgt, res in a.casts[self.n_casts:]:
            gap = "" if prev is None else f"+{t - prev:.2f}s"
            self.t_casts.insert("", 0, values=(f"{t - seg.t0:.1f}s", spell, tgt, gap, res))
            prev = t
        self.n_casts = len(a.casts)
        kicks = sorted([(t, "Interrupt", u, tg, w) for t, u, tg, w in a.interrupts] +
                       [(t, "Dispel", u, tg, w) for t, u, tg, w in a.dispels])
        for t, kind, used, tgt, what in kicks[self.n_kicks:]:
            self.t_kicks.insert("", 0, values=(f"{t - seg.t0:.1f}s", kind, used, tgt, what))
        self.n_kicks = len(kicks)

    def draw_banner(self, seg, a):
        c = self.banner; c.delete("all"); w = c.winfo_width()
        col = self.s.color(a)
        hgrad(c, 0, 0, w, 78, mix(col, BG, 0.62), BG, steps=40)
        c.create_rectangle(0, 0, 4, 78, fill=col, width=0)
        c.create_text(18, 28, text=a.name, anchor="w", fill=shade(col, 1.15), font=F["huge"])
        side = "ENEMY" if a.hostile else "FRIENDLY"
        c.create_text(19, 56, anchor="w", fill=DIM, font=F["small"],
                      text=f"{self.s.spec(a).upper()}   ·   {side}   ·   {seg.label()}")
        dur = seg.duration
        items = [("DAMAGE", fmt(a.dmg_total), f"{fmt(a.dmg_total / dur)} per sec", ACCENT),
                 ("HEALING", fmt(a.heal_total), f"{fmt(a.heal_total / dur)}/s · {fmt(a.overheal)} over", GOOD),
                 ("TAKEN", fmt(a.taken_total), f"{fmt(a.taken_total / dur)} per sec", BAD),
                 ("DEATHS", str(a.deaths), f"FF {fmt(a.ff_total)}" if a.ff_total else "", CRIT)]
        tiles(c, items, max(w - 4 * 128 - 18, 340), 12, w - 14, 56)

    def draw_chart(self, seg, a, book):
        c = self.chart; c.delete("all"); w, h = c.winfo_width(), 92
        c.create_rectangle(0, 0, w, h, fill=PANEL, width=0)
        if book is None:
            book = "dmg"
        spells = getattr(a, book)
        n = int(seg.duration) + 1
        series = [0] * n
        sel = spells.get(self.panels[book].sel)
        hl = [0] * n
        for sp in spells.values():
            for hit in sp.hit_list:
                i = int(hit[0] - seg.t0)
                if 0 <= i < n:
                    series[i] += hit[1]
                    if sp is sel:
                        hl[i] += hit[1]
        top = max(series + [1])
        x0, x1, y0, y1 = 44, w - 10, 18, h - 16
        for frac in (0.5, 1.0):
            y = y1 - (y1 - y0) * frac
            c.create_line(x0, y, x1, y, fill=LINE, dash=(2, 3))
            c.create_text(x0 - 6, y, text=fmt(top * frac), anchor="e", fill=FAINT, font=F["small"])
        bw = (x1 - x0) / n
        base = {"dmg": ACCENT, "heal": GOOD, "taken": BAD, "ff": CRIT}[book]
        for i, v in enumerate(series):
            if v:
                c.create_rectangle(x0 + i * bw + 0.5, y1 - (y1 - y0) * v / top, x0 + (i + 1) * bw - 0.5, y1,
                                   fill=mix(base, PANEL, 0.55), width=0)
            if hl[i]:
                c.create_rectangle(x0 + i * bw + 0.5, y1 - (y1 - y0) * hl[i] / top, x0 + (i + 1) * bw - 0.5, y1,
                                   fill=CRIT, width=0)
        roll = [sum(series[max(0, i - 4):i + 1]) / min(i + 1, 5) for i in range(n)]
        if n > 1:
            c.create_line(*[p for i, v in enumerate(roll) for p in (x0 + (i + .5) * bw, y1 - (y1 - y0) * v / top)],
                          fill=base, width=2, smooth=True)
        stepx = max(5, (n // 8) // 5 * 5 or 5)
        for sec in range(0, n, stepx):
            c.create_text(x0 + sec * bw, h - 7, text=f"{sec}s", fill=FAINT, font=F["small"])
        label = {"dmg": "DAMAGE", "heal": "HEALING", "taken": "DAMAGE TAKEN", "ff": "FRIENDLY FIRE"}[book]
        c.create_text(x0, 9, anchor="w", fill=DIM, font=F["small"],
                      text=f"{label} PER SECOND   ·   5s AVERAGE (LINE)" + (f"   ·   {sel.name.upper()} HIGHLIGHTED" if sel else ""))


class SpellPanel:
    """Spell list + targets on the left; stat cards and every individual hit on the right."""
    def __init__(self, nb, bd, book):
        self.bd, self.book = bd, book
        self.frame = tk.Frame(nb, bg=BG)
        pw = tk.PanedWindow(self.frame, orient="horizontal", bg=BG, sashwidth=8, bd=0)
        pw.pack(fill="both", expand=True, pady=(8, 0))
        left = tk.PanedWindow(pw, orient="vertical", bg=BG, sashwidth=8, bd=0)
        who = "Target" if book != "taken" else "Source"
        self.t_sp = make_tree(left, [("Spell" if book != "taken" else "Spell — Source", 220, "w"), ("Total", 75),
                                     ("%", 55), ("Hits", 50)])
        self.t_tg = make_tree(left, [(who, 180, "w"), ("Amount", 75), ("%", 50)])
        left.add(self.t_sp.master, height=300); left.add(self.t_tg.master)
        right = tk.Frame(pw, bg=BG)
        self.stats = tk.Canvas(right, height=150, bg=BG, highlightthickness=0); self.stats.pack(fill="x")
        over = {"dmg": "Overkill", "heal": "Overheal", "taken": "Overkill", "ff": "Overkill"}[book]
        self.t_hits = make_tree(right, [("Time", 62), (who, 130, "w"), ("Amount", 72), ("Crit", 38), ("Type", 66),
                                        (over, 66), ("Absorbed", 70), ("Mitigated", 72), ("HP after", 66)])
        self.t_hits.master.pack(fill="both", expand=True, pady=(8, 0))
        pw.add(left, width=430); pw.add(right)
        self.t_sp.bind("<<TreeviewSelect>>", self.pick)
        self.sel, self.shown = None, (None, 0)

    def reset(self):
        self.shown = (None, 0)
        self.t_hits.delete(*self.t_hits.get_children())

    def pick(self, _=None):
        sel = self.t_sp.selection()
        if sel and sel[0] != self.sel:
            self.sel = sel[0]; self.reset(); self.bd.tick(once=True)

    def refresh(self, seg, a):
        book = getattr(a, self.book)
        total = sum(sp.total for sp in book.values()) or 1
        keys = [sp.name for sp in sorted(book.values(), key=lambda sp: -sp.total)]
        t = self.t_sp
        if list(t.get_children()) != keys:
            t.delete(*t.get_children())
            for k in keys:
                t.insert("", "end", iid=k)
            if self.sel in keys:
                t.selection_set(self.sel)
        for k in keys:
            sp = book[k]
            t.item(k, values=(k, fmt(sp.total), f"{100 * sp.total / total:.1f}%", sp.hits))
        sp = book.get(self.sel)
        if sp is None:
            self.stats.delete("all")
            self.stats.create_text(self.stats.winfo_width() / 2, 75, text="select a spell on the left",
                                   fill=FAINT, font=F["title"])
            return
        tg = self.t_tg; tg.delete(*tg.get_children())
        for name, amt in sp.targets.most_common(30):
            tg.insert("", "end", values=(name, fmt(amt), f"{100 * amt / max(sp.total, 1):.0f}%"))
        self.draw_stats(seg, a, sp)
        key, done = self.shown
        if key is not sp:
            self.t_hits.delete(*self.t_hits.get_children()); done = 0
        for (ht, amt, crit, kind, over, absorbed, mit, hp, other) in sp.hit_list[done:]:
            self.t_hits.insert("", 0, values=(f"{ht - seg.t0:.2f}s", other, f"{amt:,}", "★" if crit else "", kind,
                                              fmt(over) if over else "", fmt(absorbed) if absorbed else "",
                                              fmt(mit) if mit else "", "" if hp is None else f"{hp:.0f}%"),
                                tags=("crit",) if crit else ())
        self.shown = (sp, len(sp.hit_list))

    def draw_stats(self, seg, a, sp):
        c = self.stats; c.delete("all"); w = c.winfo_width()
        n = sp.hits - sp.crits
        casts = sum(1 for x in a.casts if x[1] == sp.name) if self.book != "taken" else 0
        miss = sum(sp.misses.values())
        over = "OVERHEAL" if self.book == "heal" else "OVERKILL"
        c.create_text(2, 10, text=sp.name.upper(), anchor="w", fill=FG, font=F["title"])
        tiles(c, [("TOTAL", fmt(sp.total), f"{fmt(sp.total / seg.duration)} per sec", ACCENT),
                  ("HITS", str(sp.hits), (f"{casts} casts" if casts else "") + (f"  ·  {sp.ticks} ticks" if sp.ticks else ""), ACCENT2),
                  ("CRIT", f"{100 * sp.crits / max(sp.hits, 1):.1f}%", f"{sp.crits} of {sp.hits}", CRIT),
                  (over, fmt(sp.over), f"absorbed {fmt(sp.absorbed)}", BAD)], 0, 24, w, 58)
        tiles(c, [("NORMAL HIT", fmt(sp.n_sum / n) if n else "-",
                   f"{fmt(sp.n_min)} – {fmt(sp.n_max)}  ·  {n} hits" if n else "", "#9aa3b5"),
                  ("CRITICAL HIT", fmt(sp.c_sum / sp.crits) if sp.crits else "-",
                   f"{fmt(sp.c_min)} – {fmt(sp.c_max)}  ·  {sp.crits} crits" if sp.crits else "", CRIT),
                  ("MISSES", str(miss), ", ".join(f"{k.lower()} {v}" for k, v in sp.misses.most_common(3)), DIM)],
              0, 88, w, 58)


# =========================================================================== death recap
class DeathRecap:
    """Last 20 seconds of a player's life: HP chart with casts / debuff / buff lanes, sources and a full event log."""
    FILTERS = [("dmg", "DAMAGE", BAD), ("heal", "HEALING", GOOD), ("cast", "CASTS", ACCENT), ("aura", "AURAS", CRIT)]
    LANE_H, LANE_ROWS = 14, 3

    def __init__(self, root, state, d):
        self.d, self.T = d, d["t"]
        # auras that drop because the player died are noise - keep only those removed before the death
        self.ev = [e for e in map(self.norm, d["recap"]) if not (e[1] == "aura-" and self.T - e[0] < 0.05)]
        self.span = min(20.0, max([self.T - e[0] for e in self.ev] + [5.0]))
        self.show = {k: True for k, _, _ in self.FILTERS}
        w = self.w = toplevel(root, f"Death recap — {d['name']}", "1160x800")
        self.banner = tk.Canvas(w, height=86, bg=BG, highlightthickness=0); self.banner.pack(fill="x")
        self.chart = tk.Canvas(w, height=260, bg=BG, highlightthickness=0)
        self.chart.pack(fill="x", padx=14, pady=(10, 0))
        body = tk.PanedWindow(w, orient="horizontal", bg=BG, sashwidth=8, bd=0)
        body.pack(fill="both", expand=True, padx=14, pady=(10, 14))
        left = tk.Frame(body, bg=BG)
        tk.Label(left, text="DAMAGE SOURCES (LAST 20s)", bg=BG, fg=FAINT, font=F["small"]).pack(anchor="w", pady=(0, 4))
        self.t_src = make_tree(left, [("Source — Spell", 210, "w"), ("Total", 70), ("Hits", 45), ("%", 45)])
        self.t_src.master.pack(fill="both", expand=True)
        right = tk.Frame(body, bg=BG)
        bar = tk.Frame(right, bg=BG); bar.pack(fill="x", pady=(0, 4))
        tk.Label(bar, text="EVENT LOG", bg=BG, fg=FAINT, font=F["small"]).pack(side="left")
        self.toggles = {}
        for key, label, col in reversed(self.FILTERS):
            b = tk.Label(bar, text=label, font=F["small"], padx=10, pady=2, cursor="hand2")
            b.pack(side="right", padx=(4, 0)); b.bind("<Button-1>", lambda e, k=key: self.toggle(k))
            self.toggles[key] = (b, col)
        self.t_ev = make_tree(right, [("Before death", 100), ("Type", 80, "w"), ("From", 110, "w"), ("Spell", 190, "w"),
                                      ("Amount", 170, "w"), ("HP", 50)])
        self.t_ev.master.pack(fill="both", expand=True)
        body.add(left, width=400); body.add(right)
        self.fill_sources(); self.fill_events(); self.paint_toggles()
        w.bind("<Configure>", lambda e: e.widget is w and self.w.after_idle(self.draw))
        w.after(50, self.draw)

    @staticmethod
    def norm(e):
        if len(e) == 6:                                     # old format: (t, who, spell, amount, crit, hp)
            t, who, spell, amt, crit, hp = e
            return (t, "heal" if amt > 0 else "dmg", who, spell, amt, crit, hp, amt < 0, 0)
        return tuple(e)

    # ---------------------------------------------------------------- numbers
    def window(self, sec, kinds):
        return sum(abs(e[4]) for e in self.ev if e[1] in kinds and self.T - e[0] <= sec)

    def time_to_die(self, pct):
        above = [e[0] for e in self.ev if e[6] is not None and e[6] >= pct]
        return self.T - max(above) if above else None

    def draw(self):
        if self.w.winfo_exists():
            self.draw_banner(); self.draw_chart()

    def draw_banner(self):
        c, d = self.banner, self.d
        c.delete("all"); w = c.winfo_width()
        hgrad(c, 0, 0, w, 86, "#4a1820", BG, steps=40)
        c.create_rectangle(0, 0, 4, 86, fill=BAD, width=0)
        c.create_text(18, 30, text=f"☠  {d['name']}", anchor="w", fill="#ffd3d8", font=F["huge"])
        c.create_text(19, 62, anchor="w", fill=DIM, font=F["small"],
                      text=f"DIED AT {datetime.fromtimestamp(self.T):%H:%M:%S}   ·   {'ENEMY' if d['hostile'] else 'FRIENDLY'}")
        blow = next((e for e in reversed(self.ev) if e[1] == "dmg"), None)
        ttd80, ttd50 = self.time_to_die(80), self.time_to_die(50)
        items = [("DAMAGE TAKEN", fmt(self.window(10, ("dmg",))), f"last 10s  ·  {fmt(self.window(5, ('dmg',)))} in 5s", BAD),
                 ("HEALING RECEIVED", fmt(self.window(10, ("heal", "absorb"))),
                  f"last 10s  ·  shields {fmt(self.window(10, ('absorb',)))}", GOOD),
                 ("TIME TO DIE", f"{ttd80:.1f}s" if ttd80 is not None else "–",
                  "from 80% HP" + (f"  ·  {ttd50:.1f}s from 50%" if ttd50 is not None else ""), CRIT),
                 ("KILLING BLOW", fmt(-blow[4]) if blow else "–", f"{blow[3]} — {blow[2]}" if blow else "", ACCENT)]
        tiles(c, items, max(w - 4 * 175 - 18, 360), 14, w - 14, 58)

    # ---------------------------------------------------------------- chart
    def lanes(self):
        """Aura intervals applied inside the window -> (enemy debuffs, buffs gained)."""
        debuffs, buffs = [], []
        for i, e in enumerate(self.ev):
            if e[1] != "aura+" or self.T - e[0] < 0.05:
                continue
            end = next((x[0] for x in self.ev[i + 1:] if x[1] == "aura-" and x[3] == e[3] and x[2] == e[2]), self.T)
            if end - e[0] < 0.05:
                continue
            if e[8] == "DEBUFF" and e[7]:
                debuffs.append((e[0], end, e[3], e[2]))
            elif e[8] == "BUFF" and not e[7]:
                buffs.append((e[0], end, e[3], e[2]))
        return debuffs, buffs

    def pack_rows(self, items):
        rows, out = [], []                                  # greedy interval packing into a few rows
        for it in sorted(items):
            for n, end in enumerate(rows):
                if it[0] >= end:
                    rows[n] = it[1]; out.append((n, it)); break
            else:
                if len(rows) < self.LANE_ROWS:
                    rows.append(it[1]); out.append((len(rows) - 1, it))
        return out, max(len(rows), 1)

    def draw_chart(self):
        c = self.chart; c.delete("all"); w = c.winfo_width()
        debuffs, buffs = self.lanes()
        deb, deb_rows = self.pack_rows(debuffs)
        buf, buf_rows = self.pack_rows(buffs)
        L, R = 120, w - 14
        X = lambda t: L + (R - L) * (1 - (self.T - t) / self.span)
        y_hp0, y_hp1 = 10, 120                              # HP area
        y_flow = 152                                        # damage up / healing down around this line
        y_cast = 192
        y_deb = y_cast + 30
        y_buf = y_deb + deb_rows * self.LANE_H + 14
        H = y_buf + buf_rows * self.LANE_H + 26
        c.config(height=H)
        c.create_rectangle(0, 0, w, H, fill=PANEL, width=0)
        for lab, y in (("HP", (y_hp0 + y_hp1) / 2), ("DAMAGE / HEAL", y_flow), ("CASTS", y_cast),
                       ("ENEMY DEBUFFS", y_deb + 6), ("BUFFS GAINED", y_buf + 6)):
            c.create_text(12, y, text=lab, anchor="w", fill=FAINT, font=F["small"])
        for sec in range(0, int(self.span) + 1, 2):         # time grid
            x = X(self.T - sec)
            c.create_line(x, y_hp0, x, H - 18, fill=LINE, dash=(1, 4))
            c.create_text(x, H - 9, text=f"-{sec}s" if sec else "death", fill=BAD if not sec else FAINT, font=F["small"])
        for pct in (25, 50, 75, 100):
            y = y_hp1 - (y_hp1 - y_hp0) * pct / 100
            c.create_line(L, y, R, y, fill=LINE, dash=(2, 3))
            c.create_text(L - 6, y, text=f"{pct}%", anchor="e", fill=FAINT, font=F["small"])
        # HP line, coloured by health
        Y = lambda hp: y_hp1 - (y_hp1 - y_hp0) * hp / 100
        pts = [(e[0], e[6]) for e in self.ev if e[6] is not None and self.T - e[0] <= self.span] + [(self.T, 0.0)]
        if len(pts) > 1:
            poly = [X(pts[0][0]), y_hp1]
            for t, hp in pts:
                poly += [X(t), Y(hp)]
            c.create_polygon(poly + [X(self.T), y_hp1], fill=mix(GOOD, PANEL, 0.82), outline="")
            for (t0, h0), (t1, h1) in zip(pts, pts[1:]):
                c.create_line(X(t0), Y(h0), X(t1), Y(h1), fill=mix(BAD, GOOD, min(max(h1, 0), 100) / 100), width=2)
        # damage (up) and healing (down) per 0.5s
        n = int(self.span * 2) + 1
        dmg, heal = [0] * n, [0] * n
        for e in self.ev:
            i = int((self.T - e[0]) * 2)
            if 0 <= i < n:
                if e[1] == "dmg":
                    dmg[i] += -e[4]
                elif e[1] in ("heal", "absorb"):
                    heal[i] += e[4]
        top = max(dmg + heal + [1])
        bw = (R - L) / (self.span * 2)
        c.create_line(L, y_flow, R, y_flow, fill=LINE)
        for i in range(n):
            x1 = X(self.T - i / 2); x0 = max(L, x1 - bw + 1)
            if dmg[i]:
                c.create_rectangle(x0, y_flow - 24 * dmg[i] / top, x1, y_flow, fill=BAD, width=0)
            if heal[i]:
                c.create_rectangle(x0, y_flow, x1, y_flow + 24 * heal[i] / top, fill=GOOD, width=0)
        c.create_text(R, y_flow - 20, text=f"peak {fmt(max(dmg))} dmg / 0.5s", anchor="e", fill=FAINT, font=F["small"])
        # casts lane (two label rows so names don't collide)
        last_x = [-999.0, -999.0]
        for e in self.ev:
            if e[1] not in ("cast", "kicked") or self.T - e[0] > self.span:
                continue
            x = X(e[0]); kicked = e[1] == "kicked"
            col = BAD if kicked else ACCENT2
            c.create_polygon(x, y_cast - 5, x + 5, y_cast, x, y_cast + 5, x - 5, y_cast, fill=col, outline="")
            row = 0 if x - last_x[0] > 70 else 1
            if x - last_x[row] > 70:
                label = f"✖ {e[8]}" if kicked else e[3]
                c.create_text(x, y_cast + (-12 if row == 0 else 13), text=fit(label, F["small"], 110),
                              fill=col if kicked else DIM, font=F["small"])
                last_x[row] = x
        for (n, (t0, t1, spell, who)), y, col in [(x, y_deb, BAD) for x in deb] + [(x, y_buf, GOOD) for x in buf]:
            yy = y + n * self.LANE_H
            x0, x1 = X(max(t0, self.T - self.span)), X(t1)
            c.create_rectangle(x0, yy, max(x1, x0 + 3), yy + self.LANE_H - 3, fill=mix(col, PANEL, 0.55), width=0)
            c.create_rectangle(x0, yy, x0 + 2, yy + self.LANE_H - 3, fill=col, width=0)
            if x1 - x0 > 30:
                c.create_text(x0 + 5, yy + (self.LANE_H - 3) / 2, text=fit(f"{spell} ({who})", F["small"], x1 - x0 - 8),
                              anchor="w", fill=FG, font=F["small"])

    # ---------------------------------------------------------------- tables
    def fill_sources(self):
        agg = {}
        for e in self.ev:
            if e[1] == "dmg":
                a = agg.setdefault(f"{e[2]} — {e[3]}", [0, 0]); a[0] += -e[4]; a[1] += 1
        total = sum(v[0] for v in agg.values()) or 1
        for k, (amt, hits) in sorted(agg.items(), key=lambda x: -x[1][0]):
            self.t_src.insert("", "end", values=(k, fmt(amt), hits, f"{100 * amt / total:.0f}%"))

    def toggle(self, key):
        self.show[key] = not self.show[key]
        self.paint_toggles(); self.fill_events()

    def paint_toggles(self):
        for key, (b, col) in self.toggles.items():
            on = self.show[key]
            b.config(bg=mix(col, PANEL, 0.6) if on else PANEL, fg=FG if on else FAINT)

    def fill_events(self):
        t = self.t_ev; t.delete(*t.get_children())
        group = {"dmg": "dmg", "heal": "heal", "absorb": "heal", "cast": "cast", "kicked": "cast",
                 "aura+": "aura", "aura-": "aura"}
        for (et, kind, who, spell, amt, crit, hp, hostile, extra) in reversed(self.ev):
            if not self.show[group.get(kind, "aura")]:
                continue
            if kind == "dmg":
                typ, tag = ("Crit hit" if crit else "Hit"), "bad"
                amount = f"-{-amt:,}" + (f"   ({fmt(extra)} absorbed)" if extra else "")
            elif kind == "heal":
                typ, tag = ("Crit heal" if crit else "Heal"), "good"
                amount = f"+{amt:,}" + (f"   ({fmt(extra)} overheal)" if extra else "")
            elif kind == "absorb":
                typ, tag, amount = "Shield", "good", f"+{amt:,} absorbed"
            elif kind == "cast":
                typ, tag, amount = "Cast", "cast", f"→ {extra}" if extra else ""
            elif kind == "kicked":
                typ, tag, amount = "Interrupted", "bad", f"lost {extra}"
            else:
                debuff = extra == "DEBUFF"
                typ = ("Debuff " if debuff else "Buff ") + ("+" if kind == "aura+" else "−")
                tag = "crit" if debuff and hostile else "dim"
                amount = ""
            t.insert("", "end", values=(f"-{self.T - et:.2f}s", typ, who, spell + (" ★" if crit else ""), amount,
                                        "" if hp is None else f"{hp:.0f}%"), tags=(tag,))


# =========================================================================== history browser
class History:
    """Every saved segment from combat.db. Open one to view it in the meter (and click through as usual)."""
    def __init__(self, meter):
        self.m = meter
        w = self.w = toplevel(meter.root, "Fight history", "1000x560")
        c = tk.Canvas(w, height=56, bg=BG, highlightthickness=0); c.pack(fill="x")
        w.update_idletasks()
        hgrad(c, 0, 0, max(w.winfo_width(), 1000), 56, mix(ACCENT, BG, 0.65), BG, steps=36)
        c.create_rectangle(0, 0, 4, 56, fill=ACCENT, width=0)
        c.create_text(18, 28, text="◷  FIGHT HISTORY", anchor="w", fill=FG, font=F["big"])
        top = tk.Frame(w, bg=BG); top.pack(fill="x", padx=14, pady=(10, 8))
        tk.Label(top, text="SEARCH", bg=BG, fg=FAINT, font=F["small"]).pack(side="left")
        self.q = tk.Entry(top, bg=PANEL2, fg=FG, insertbackground=FG, relief="flat", width=32, font=F["ui"])
        self.q.pack(side="left", padx=10, ipady=5)
        self.q.bind("<KeyRelease>", lambda e: self.load())
        button(top, "DELETE", self.delete, BAD).pack(side="right", padx=(8, 0))
        button(top, "OPEN IN METER", self.open).pack(side="right")
        self.l_count = tk.Label(top, bg=BG, fg=DIM, font=F["small"]); self.l_count.pack(side="right", padx=12)
        self.t = make_tree(w, [("Date", 90), ("Time", 60), ("Fight", 260, "w"), ("Length", 60), ("Players", 65),
                               ("Your damage", 150), ("Top damage", 220, "w"), ("Deaths", 60)])
        self.t.master.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.t.bind("<Double-1>", lambda e: self.open())
        self.load()

    def load(self):
        self.m.store.flush()
        t = self.t; t.delete(*t.get_children())
        rows = store.list_segments(self.q.get().strip())
        for sid, opened, label, dur, forced, players, my_dmg, top, deaths in rows:
            when = datetime.fromtimestamp(opened)
            name = label.split("  ", 1)[-1].rsplit("  (", 1)[0]
            mine = f"{fmt(my_dmg)}  ({fmt((my_dmg or 0) / dur)}/s)" if my_dmg else "-"
            tname, tdmg = (top or "?|0").rsplit("|", 1)
            t.insert("", "end", iid=str(sid), values=(f"{when:%Y-%m-%d}", f"{when:%H:%M}", name, mmss(dur), players,
                                                     mine, f"{tname}  {fmt(int(tdmg) / dur)}/s", deaths or ""))
        self.l_count.config(text=f"{len(rows)} FIGHTS")

    def selected(self):
        sel = self.t.selection()
        return int(sel[0]) if sel else None

    def open(self):
        sid = self.selected()
        if sid is None:
            return
        seg = store.load_segment(sid)
        if seg:
            self.m.set_view(seg)
            self.m.root.lift()

    def delete(self):
        sid = self.selected()
        if sid is None:
            return
        name = self.t.item(str(sid), "values")[2]
        if messagebox.askyesno("Delete fight", f"Permanently delete '{name}' from history?", parent=self.w):
            store.delete_segment(sid)
            self.load()


# =========================================================================== arena record
class Arena:
    """Wins / losses per bracket (Solo Shuffle counted in rounds), spec matchups and every match."""
    BRACKETS = [("Solo Shuffle", ACCENT), ("2v2", ACCENT2), ("3v3", CRIT)]
    PERIODS = [("TODAY", "today"), ("7 DAYS", "7d"), ("ALL TIME", None)]

    def __init__(self, meter):
        self.m, self.s = meter, meter.s
        self.period, self.bracket, self.seen = None, None, -1
        chars = store.arena_characters()
        me = self.s.my_name
        self.char = me if me in chars or not chars else chars[0]
        w = self.w = toplevel(meter.root, "Arena record", "1200x820")
        self.banner = tk.Canvas(w, height=64, bg=BG, highlightthickness=0); self.banner.pack(fill="x")
        self.banner.tag_bind("char", "<Button-1>", lambda e: self.char_menu())
        for _, key in self.PERIODS:
            self.banner.tag_bind(f"p_{key}", "<Button-1>", lambda e, k=key: self.set_period(k))
        self.cards = tk.Canvas(w, height=128, bg=BG, highlightthickness=0); self.cards.pack(fill="x", padx=14, pady=(10, 0))
        self.cards.bind("<Button-1>", self.card_click)
        self.all = []
        for cv in (self.banner, self.cards):                 # redraw once laid out / when resized
            cv.bind("<Configure>", lambda e: self.redraw_top())
        mid = tk.Frame(w, bg=BG); mid.pack(fill="x", padx=14, pady=(10, 0))
        mid.columnconfigure(0, weight=1, uniform="m"); mid.columnconfigure(1, weight=1, uniform="m")
        self.t_vs = self.spec_table(mid, "VS ENEMY SPECS", 0)
        self.t_with = self.spec_table(mid, "WITH TEAMMATE SPECS", 1)
        tk.Label(w, text="MATCHES   ·   expand a Solo Shuffle for its rounds   ·   double-click to open in the meter",
                 bg=BG, fg=FAINT, font=F["small"]).pack(anchor="w", padx=14, pady=(10, 4))
        fr = tk.Frame(w, bg=BG); fr.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        cols = [("When", 120, "w"), ("Bracket", 95, "w"), ("Map", 150, "w"), ("Result", 75, "w"), ("Score", 60),
                ("Length", 60), ("Team", 300, "w"), ("Enemies", 300, "w")]
        self.t = ttk.Treeview(fr, columns=[c[0] for c in cols], show="tree headings", selectmode="browse")
        self.t.column("#0", width=26, stretch=False)
        for name, width, anchor in [(c[0], c[1], c[2] if len(c) > 2 else "e") for c in cols]:
            self.t.heading(name, text=name.upper(), anchor=anchor)
            self.t.column(name, width=width, anchor=anchor, stretch=anchor == "w")
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.t.yview); self.t.configure(yscrollcommand=sb.set)
        self.t.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        for tag, col in (("win", GOOD), ("loss", BAD), ("even", CRIT), ("draw", DIM)):
            self.t.tag_configure(tag, foreground=col)
        self.t.bind("<Double-1>", self.open_row)
        self.rounds = {}
        self.poll()

    def spec_table(self, master, title, col):
        fr = tk.Frame(master, bg=BG); fr.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
        tk.Label(fr, text=title, bg=BG, fg=FAINT, font=F["small"]).pack(anchor="w", pady=(0, 4))
        t = make_tree(fr, [("Spec", 220, "w"), ("Won", 60), ("Lost", 60), ("Win %", 70), ("", 160, "w")], height=7)
        t.master.pack(fill="both", expand=True)
        for cls, color in CLASS_COLOR.items():
            t.tag_configure(cls, foreground=color)
        return t

    # ---------------------------------------------------------------- data
    def poll(self):
        if not self.w.winfo_exists():
            return
        n = len(self.s.matches)
        if n != self.seen:                                   # a match just finished (or first open)
            self.seen = n; self.m.store.flush(); self.load()
        self.w.after(2000, self.poll)

    def since(self):
        if self.period == "today":
            return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if self.period == "7d":
            return time.time() - 7 * 86400
        return None

    def load(self):
        self.all = store.list_matches(self.char, self.since())
        shown = [m for m in self.all if self.bracket in (None, m["bracket"])]
        self.draw_banner(); self.draw_cards(self.all)
        self.fill_specs(shown); self.fill_matches(shown)

    @staticmethod
    def units(m):
        """The things that count as a win/loss: rounds in Shuffle, the match otherwise."""
        if m["bracket"] == "Solo Shuffle":
            return m["rounds"]
        r = dict(m["rounds"][0]); r["result"] = {"even": "draw"}.get(m["result"], m["result"])
        return [r]

    # ---------------------------------------------------------------- drawing
    def redraw_top(self):
        if self.w.winfo_exists():
            self.draw_banner(); self.draw_cards(self.all)

    def draw_banner(self):
        c = self.banner; c.delete("all"); w = max(c.winfo_width(), 900)
        hgrad(c, 0, 0, w, 64, mix(ACCENT, BG, 0.62), BG, steps=40)
        c.create_rectangle(0, 0, 4, 64, fill=ACCENT, width=0)
        c.create_text(18, 32, text="⚔  ARENA RECORD", anchor="w", fill=FG, font=F["big"])
        name = (self.char or "no arena games yet").split("-")[0] + "  ▾"
        x = 18 + F["big"].measure("⚔  ARENA RECORD") + 24
        pill(c, x, 20, x + F["title"].measure(name) + 24, 44, PANEL2, outline=LINE, tags=("char",))
        c.create_text(x + 12, 32, text=name, anchor="w", fill=FG, font=F["title"], tags=("char",))
        x = w - 14
        for label, key in reversed(self.PERIODS):
            tw = F["small"].measure(label) + 22
            on = key == self.period
            pill(c, x - tw, 21, x, 43, mix(ACCENT, PANEL, 0.45) if on else PANEL2, outline=LINE, tags=(f"p_{key}",))
            c.create_text(x - tw / 2, 32, text=label, fill=FG if on else DIM, font=F["small"], tags=(f"p_{key}",))
            x -= tw + 6

    def draw_cards(self, matches):
        c = self.cards; c.delete("all"); w = max(c.winfo_width(), 900)
        agg = {b: {"w": 0, "l": 0, "d": 0, "games": 0, "lw": 0, "ll": 0, "le": 0} for b, _ in self.BRACKETS}
        form = []
        for m in sorted(matches, key=lambda m: m["start"]):
            a = agg.setdefault(m["bracket"], {"w": 0, "l": 0, "d": 0, "games": 0, "lw": 0, "ll": 0, "le": 0})
            a["games"] += 1
            a[{"win": "lw", "loss": "ll"}.get(m["result"], "le")] += 1
            for u in self.units(m):
                a[{"win": "w", "loss": "l"}.get(u["result"], "d")] += 1
                form.append(u["result"])
        self.card_boxes = []
        gap, n = 10, len(self.BRACKETS)
        cw = (w - gap * n - 260) / n
        for i, (b, col) in enumerate(self.BRACKETS):
            a = agg[b]; x0 = i * (cw + gap); x1 = x0 + cw
            sel = self.bracket == b
            c.create_rectangle(x0, 0, x1, 84, fill=mix(col, PANEL2, 0.82) if sel else PANEL2, width=0)
            c.create_rectangle(x0, 0, x1, 3, fill=col, width=0)
            played = a["w"] + a["l"]
            pct = 100 * a["w"] / played if played else 0
            c.create_text(x0 + 14, 18, text=b.upper() + ("   ·   ROUNDS" if b == "Solo Shuffle" else ""), anchor="w",
                          fill=FAINT, font=F["small"])
            c.create_text(x0 + 14, 46, text=f"{a['w']} – {a['l']}", anchor="w", fill=FG, font=F["huge"])
            c.create_text(x1 - 14, 46, text=f"{pct:.0f}%" if played else "–", anchor="e",
                          fill=GOOD if pct >= 50 and played else BAD if played else FAINT, font=F["big"])
            sub = (f"{a['games']} lobbies   ·   {a['lw']} up  {a['ll']} down  {a['le']} even" if b == "Solo Shuffle"
                   else f"{a['games']} games" + (f"   ·   {a['d']} draws" if a["d"] else ""))
            c.create_text(x0 + 14, 72, text=sub, anchor="w", fill=DIM, font=F["small"])
            c.create_rectangle(x0, 80, x1, 84, fill=PANEL, width=0)                   # win-rate bar
            if played:
                c.create_rectangle(x0, 80, x0 + cw * pct / 100, 84, fill=GOOD, width=0)
                c.create_rectangle(x0 + cw * pct / 100, 80, x1, 84, fill=BAD, width=0)
            self.card_boxes.append((x0, x1, b))
        # recent form: newest on the right
        fx0 = w - 250
        c.create_rectangle(fx0, 0, w, 84, fill=PANEL2, width=0)
        c.create_rectangle(fx0, 0, w, 3, fill=GOOD, width=0)
        c.create_text(fx0 + 14, 18, text="RECENT FORM", anchor="w", fill=FAINT, font=F["small"])
        recent = form[-30:]
        for j, res in enumerate(recent):
            col = {"win": GOOD, "loss": BAD}.get(res, FAINT)
            cx = fx0 + 14 + (j % 15) * 15; cy = 34 + (j // 15) * 16
            c.create_rectangle(cx, cy, cx + 11, cy + 11, fill=col, width=0)
        streak = 0
        for res in reversed(form):
            if form and res == form[-1]:
                streak += 1
            else:
                break
        if form:
            c.create_text(fx0 + 14, 74, anchor="w", fill=DIM, font=F["small"],
                          text=f"current streak: {streak} {'win' if form[-1] == 'win' else 'loss' if form[-1] == 'loss' else 'draw'}"
                               + ("s" if streak != 1 else "") + "   ·   newest →")
        c.create_text(0, 108, anchor="w", fill=FAINT, font=F["small"],
                      text="click a bracket to filter" + (f"   ·   showing {self.bracket} only" if self.bracket else ""))

    def card_click(self, e):
        for x0, x1, b in getattr(self, "card_boxes", []):
            if x0 <= e.x <= x1 and e.y <= 84:
                self.bracket = None if self.bracket == b else b
                self.load(); return

    def fill_specs(self, matches):
        vs, with_ = {}, {}
        for m in matches:
            for u in self.units(m):
                if u["result"] not in ("win", "loss"):
                    continue
                for p in u["enemies"]:
                    vs.setdefault(p["spec"], [0, 0, p.get("cls")])[u["result"] == "loss"] += 1
                for p in u["team"]:
                    if not p.get("me"):
                        with_.setdefault(p["spec"], [0, 0, p.get("cls")])[u["result"] == "loss"] += 1
        for t, data in ((self.t_vs, vs), (self.t_with, with_)):
            t.delete(*t.get_children())
            for spec, (won, lost, cls) in sorted(data.items(), key=lambda x: -(x[1][0] + x[1][1])):
                pct = 100 * won / max(won + lost, 1)
                bar = "▰" * round(pct / 10) + "▱" * (10 - round(pct / 10))
                t.insert("", "end", values=(spec, won, lost, f"{pct:.0f}%", bar), tags=(cls,) if cls else ())

    def fill_matches(self, matches):
        t = self.t; t.delete(*t.get_children()); self.rounds = {}
        comp = lambda ps: ",  ".join(f"{p['name']} ({p['spec']})" for p in ps if not p.get("me"))
        for m in matches:
            when = datetime.fromtimestamp(m["start"])
            shuffle = m["bracket"] == "Solo Shuffle"
            res = {"win": "WIN", "loss": "LOSS", "even": "EVEN"}[m["result"]]
            if shuffle:
                players = {p["name"]: p for r in m["rounds"] for p in r["team"] + r["enemies"] if not p.get("me")}
                team, enemies = f"{len(players)} other players", comp(players.values())
                score = f"{m['rw']}-{m['rl']}"
            else:
                team, enemies = comp(m["rounds"][0]["team"]), comp(m["rounds"][0]["enemies"])
                score = ""
            iid = t.insert("", "end", values=(f"{when:%a %d %b  %H:%M}", m["bracket"], m["zone"] or "", res, score,
                                              mmss(m["duration"]), team, enemies), tags=(m["result"],))
            self.rounds[iid] = m["rounds"][0]["opened"]
            if shuffle:
                for k, r in enumerate(m["rounds"], 1):
                    died = (f"{r['died']} died ({r['killer']})" if r["died"] else "no death")
                    rid = t.insert(iid, "end", values=(f"   round {k}", "", died, r["result"].upper(), "",
                                                       mmss(r["duration"]), comp(r["team"]), comp(r["enemies"])),
                                   tags=(r["result"],))
                    self.rounds[rid] = r["opened"]

    # ---------------------------------------------------------------- actions
    def open_row(self, e):
        iid = self.t.identify_row(e.y)
        if not iid or iid not in self.rounds:
            return
        self.m.store.flush()
        sid = store.segment_at(self.rounds[iid])
        seg = store.load_segment(sid) if sid else None
        if seg:
            self.m.set_view(seg); self.m.root.lift()

    def set_period(self, key):
        self.period = key; self.load()

    def char_menu(self):
        m = self.m.menu()
        for name in store.arena_characters():
            m.add_command(label=("  ●  " if name == self.char else "      ") + name,
                          command=lambda n=name: (setattr(self, "char", n), self.load()))
        self.m.popup(m)



if __name__ == "__main__":
    state = State()
    history = store.Store(state)          # saves every segment as it closes
    start_tail(state, replay="--live" not in sys.argv)
    root = tk.Tk()
    root.title("WoW Live Meter")

    def log_error(*exc):        # pythonw has no console, so keep UI errors in a file
        import traceback
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S}\n{''.join(traceback.format_exception(*exc))}\n")
    root.report_callback_exception = log_error
    Meter(root, state, history)
    root.mainloop()
    with state.lock:                      # save the fight that is still open
        state.close()
    history.flush()
