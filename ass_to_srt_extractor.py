import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Drag & drop support needs tkinterdnd2 – install it automatically if missing
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    import subprocess
    import sys
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'tkinterdnd2'])
        from tkinterdnd2 import DND_FILES, TkinterDnD
        HAS_DND = True
    except Exception:
        HAS_DND = False

# ───────────────────────────── ASS parsing ─────────────────────────────

TAG_RE = re.compile(r'\{.*?\}')
DRAWING_RE = re.compile(r'\{[^}]*\\p[1-9][^}]*\}')
KARAOKE_RE = re.compile(r'\\k[fo]?\d')
POS_RE = re.compile(r'\\(pos|move|org|fad|fade|clip|iclip)\b|\\pos\(')

# Style names that usually mean songs / signs / extras
SONG_NAME_RE = re.compile(
    r'(^|[^a-z])(op|ed|opening|ending|song|songs|kara|karaoke|romaji|kanji|lyric|lyrics|insert|'
    r'jp|jap|japanese|eng ?song|translation|trans|credits?)([^a-z]|$)', re.I)
SIGN_NAME_RE = re.compile(r'(sign|title|text ?on|screen|note|caption|eyecatch|next|preview|logo|staff)', re.I)
DIALOGUE_NAME_RE = re.compile(
    r'(default|main|dialog|dialogue|italic|narrat|thought|internal|flashback|top|overlap|speech|subtitle|sub)',
    re.I)


def format_time(ass_time):
    """ASS time (H:MM:SS.cs) -> SRT time (HH:MM:SS,mls)"""
    try:
        h_m_s, cs = ass_time.split('.')
        h, m, s = h_m_s.split(':')
        return f"{h.zfill(2)}:{m.zfill(2)}:{s.zfill(2)},{cs.ljust(3, '0')[:3]}"
    except ValueError:
        return "00:00:00,000"


def time_to_ms(ass_time):
    try:
        h_m_s, cs = ass_time.split('.')
        h, m, s = (int(x) for x in h_m_s.split(':'))
        return ((h * 60 + m) * 60 + s) * 1000 + int(cs.ljust(3, '0')[:3])
    except ValueError:
        return 0


def clean_text(text):
    """Strip ASS override tags, convert line breaks / hard spaces"""
    text = TAG_RE.sub('', text)
    text = text.replace(r'\N', '\n').replace(r'\n', '\n').replace(r'\h', ' ')
    lines = [ln.strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def read_ass(filepath):
    for enc in ('utf-8-sig', 'utf-16', 'cp1252'):
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return f.read().splitlines()
        except (UnicodeError, UnicodeDecodeError):
            continue
    raise ValueError("Unsupported file encoding")


def parse_events(filepath):
    """Return list of event dicts for every Dialogue line (Comments are skipped)."""
    events = []
    fmt = ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr',
           'marginv', 'effect', 'text']
    in_events = False

    for line in read_ass(filepath):
        stripped = line.strip()
        if stripped.startswith('['):
            in_events = stripped.lower() == '[events]'
            continue
        if not in_events:
            continue
        if stripped.lower().startswith('format:'):
            fmt = [c.strip().lower() for c in stripped.split(':', 1)[1].split(',')]
            continue
        if not stripped.startswith('Dialogue:'):
            continue

        parts = stripped.split(':', 1)[1].lstrip().split(',', len(fmt) - 1)
        if len(parts) < len(fmt):
            continue
        row = dict(zip(fmt, parts))
        raw = row.get('text', '')
        events.append({
            'start': row.get('start', '').strip(),
            'end': row.get('end', '').strip(),
            'style': row.get('style', '').strip(),
            'raw': raw,
            'text': clean_text(raw),
            'is_drawing': bool(DRAWING_RE.search(raw)),
            'is_karaoke': bool(KARAOKE_RE.search(raw)),
            'has_pos': bool(POS_RE.search(raw)),
        })
    return events


def analyse_style(name, evs):
    """Guess what kind of content a style holds. Returns (kind, auto_checked)."""
    n = len(evs)
    kara = sum(e['is_karaoke'] for e in evs) / n
    draw = sum(e['is_drawing'] for e in evs) / n
    pos = sum(e['has_pos'] for e in evs) / n

    if draw > 0.5:
        return "Drawing / FX", False
    if kara > 0.3 or SONG_NAME_RE.search(name):
        return "Song (OP/ED)", False
    if pos > 0.6 or SIGN_NAME_RE.search(name):
        return "Sign / Title", False
    if DIALOGUE_NAME_RE.search(name):
        return "Dialogue", True
    return "Other", True


def smart_levels(fs):
    """Fill every style of one file with 'kinds' and 'picks' for Smart pick levels 1-3.

    fs: {style: {'events': [...]}}
    """
    starts = [time_to_ms(e['start']) for d in fs.values() for e in d['events']]
    ends = [time_to_ms(e['end']) for d in fs.values() for e in d['events']]
    t0, t1 = (min(starts), max(ends)) if starts else (0, 1)
    extent = max(t1 - t0, 1)

    for name, d in fs.items():
        evs = d['events']
        n = len(evs)
        kind1, pick1 = analyse_style(name, evs)
        span = (max(time_to_ms(e['end']) for e in evs) -
                min(time_to_ms(e['start']) for e in evs)) / extent
        pos = sum(e['has_pos'] for e in evs) / n
        d['span'], d['pos'] = span, pos
        kind2, pick2 = kind1, pick1
        # a style that only lives in a short window of a normal-length episode
        if pick2 and n >= 8 and span < 0.25 and extent > 5 * 60 * 1000:
            kind2, pick2 = "Burst (timing)", False
        d['kinds'] = {1: kind1, 2: kind2}
        d['picks'] = {1: pick1, 2: pick2}

    # styles whose lines start at the same moments as an already rejected style → lyrics pair
    rejected = set()
    for d in fs.values():
        if not d['picks'][2]:
            rejected.update(time_to_ms(e['start']) for e in d['events'])
    for d in fs.values():
        if d['picks'][2] and len(d['events']) >= 5:
            same = sum(time_to_ms(e['start']) in rejected for e in d['events'])
            if same / len(d['events']) >= 0.7:
                d['kinds'][2], d['picks'][2] = "Song (matched)", False

    for name, d in fs.items():
        kind3, pick3 = d['kinds'][2], d['picks'][2]
        if pick3:
            dialogue_named = bool(DIALOGUE_NAME_RE.search(name))
            runs_whole_episode = d['span'] >= 0.5 and len(d['events']) >= 20
            if d['pos'] >= 0.2 or not (dialogue_named or runs_whole_episode):
                kind3, pick3 = "Extra / unclear", False
        d['kinds'][3], d['picks'][3] = kind3, pick3


# ───────────────────────────── Theme ─────────────────────────────

BG = "#1e1f2b"
PANEL = "#272a3a"
PANEL_ALT = "#2d3045"
FG = "#e6e8f2"
MUTED = "#8b90a8"
ACCENT = "#6c8cff"
ACCENT_HOVER = "#8aa3ff"
GOOD = "#4cd9a0"
WARN = "#ffb454"
BAD = "#ff6b81"

CHECKED = "☑"
UNCHECKED = "☐"
MIXED = "▣"

KIND_COLORS = {
    "Dialogue": GOOD,
    "Other": FG,
    "Song (OP/ED)": WARN,
    "Sign / Title": WARN,
    "Drawing / FX": BAD,
    "Burst (timing)": WARN,
    "Song (matched)": WARN,
    "Extra / unclear": MUTED,
}

LEVEL_LABELS = ["① Quick", "② Balanced", "③ Strict"]
LEVEL_DESC = {
    1: "Level 1 – Quick: judges each style by its name and its tags (karaoke, drawings, "
       "positioning). Fast, but trusts style names.",
    2: "Level 2 – Balanced: Level 1 + timing. Drops styles that only appear in a short burst "
       "(OP/ED/signs) and styles whose lines line up with a song style (e.g. English lyrics "
       "with no tags).",
    3: "Level 3 – Strict: Level 2 + keeps only styles that run through the whole episode "
       "without positioning, or are clearly named as dialogue. Best for a clean dialogue-only SRT.",
}


class App:
    def __init__(self, root):
        self.root = root
        root.title("ASS → SRT  •  Dialogue Extractor")
        root.geometry("1100x740")
        root.minsize(900, 620)
        root.configure(bg=BG)

        self.files = []            # list of file paths
        self.events = {}           # path -> list of events
        self.file_styles = {}      # path -> {style: {'events', 'kind', 'auto'}}
        self.checked = {}          # path -> {style: bool}
        self.scope = []            # files currently shown in the style table
        self.view = {}
        self.out_dir = tk.StringVar()

        self._setup_style()
        self._build_ui()

        self.rebuild()

        if HAS_DND:
            for w in (root, self.file_list):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self._on_drop)

    # ─────────── styling ───────────
    def _setup_style(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('.', background=BG, foreground=FG, font=('Segoe UI', 10))
        s.configure('TFrame', background=BG)
        s.configure('Card.TFrame', background=PANEL)
        s.configure('TLabel', background=BG, foreground=FG)
        s.configure('Card.TLabel', background=PANEL, foreground=FG)
        s.configure('Title.TLabel', font=('Segoe UI', 18, 'bold'), foreground=FG)
        s.configure('Sub.TLabel', foreground=MUTED)
        s.configure('Head.TLabel', background=PANEL, font=('Segoe UI', 11, 'bold'))
        s.configure('TButton', background=PANEL_ALT, foreground=FG, borderwidth=0,
                    padding=(12, 6))
        s.map('TButton', background=[('active', '#3a3e5a')])
        s.configure('Accent.TButton', background=ACCENT, foreground='white',
                    font=('Segoe UI', 11, 'bold'), padding=(18, 9))
        s.map('Accent.TButton', background=[('active', ACCENT_HOVER),
                                            ('disabled', '#3a3e5a')],
              foreground=[('disabled', MUTED)])
        s.configure('TEntry', fieldbackground=PANEL_ALT, foreground=FG,
                    insertcolor=FG, borderwidth=0, padding=6)
        s.configure('Treeview', background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=30, borderwidth=0)
        s.configure('Treeview.Heading', background=PANEL_ALT, foreground=MUTED,
                    font=('Segoe UI', 9, 'bold'), borderwidth=0, padding=6)
        s.map('Treeview', background=[('selected', '#3b4170')],
              foreground=[('selected', 'white')])
        s.map('Treeview.Heading', background=[('active', PANEL_ALT)])
        s.configure('TCombobox', fieldbackground=PANEL_ALT, background=PANEL_ALT,
                    foreground=FG, arrowcolor=FG, borderwidth=0, padding=4)
        s.map('TCombobox', fieldbackground=[('readonly', PANEL_ALT)],
              foreground=[('readonly', FG)], selectbackground=[('readonly', PANEL_ALT)],
              selectforeground=[('readonly', FG)])
        self.root.option_add('*TCombobox*Listbox.background', PANEL_ALT)
        self.root.option_add('*TCombobox*Listbox.foreground', FG)
        self.root.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        s.configure('Vertical.TScrollbar', background=PANEL_ALT, troughcolor=PANEL,
                    borderwidth=0, arrowcolor=MUTED)
        s.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=PANEL_ALT,
                    borderwidth=0)

    # ─────────── layout ───────────
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill='both', expand=True)

        header = ttk.Frame(outer)
        header.pack(fill='x')
        ttk.Label(header, text="ASS → SRT Dialogue Extractor",
                  style='Title.TLabel').pack(anchor='w')
        hint = ("Add .ass files (or drag & drop), tick only the styles you want, "
                "then convert.") if HAS_DND else \
               "Add .ass files, tick only the styles you want, then convert."
        ttk.Label(header, text=hint, style='Sub.TLabel').pack(anchor='w', pady=(2, 12))

        body = ttk.Frame(outer)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=2, minsize=290)
        body.columnconfigure(1, weight=5)
        body.rowconfigure(0, weight=1)

        # ── Left: files ──
        left = ttk.Frame(body, style='Card.TFrame', padding=12)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        ttk.Label(left, text="1 · Subtitle files", style='Head.TLabel').pack(anchor='w')

        lf = ttk.Frame(left, style='Card.TFrame')
        lf.pack(fill='both', expand=True, pady=8)
        self.file_list = tk.Listbox(
            lf, bg=PANEL_ALT, fg=FG, selectbackground='#3b4170', selectforeground='white',
            relief='flat', highlightthickness=0, activestyle='none',
            font=('Segoe UI', 9), selectmode='extended', exportselection=False)
        sb = ttk.Scrollbar(lf, orient='vertical', command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=sb.set)
        self.file_list.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.file_list.bind('<<ListboxSelect>>', self._on_scope_change)
        self.file_list.bind('<Delete>', lambda e: self.remove_files())
        self.file_list.bind('<BackSpace>', lambda e: self.remove_files())
        self.file_list.bind('<Button-3>', self._on_file_right_click)
        self.file_menu = tk.Menu(self.root, tearoff=0, bg=PANEL_ALT, fg=FG,
                                 activebackground=ACCENT, activeforeground='white', bd=0)
        self.file_menu.add_command(label="🗑  Remove from list", command=self.remove_files)

        bf = ttk.Frame(left, style='Card.TFrame')
        bf.pack(fill='x')
        ttk.Button(bf, text="＋ Add files", command=self.add_files).pack(side='left')
        ttk.Button(bf, text="Remove", command=self.remove_files).pack(side='left', padx=6)
        ttk.Button(bf, text="Clear", command=self.clear_files).pack(side='left')
        self.file_count = ttk.Label(left, text="No files loaded", style='Card.TLabel',
                                    foreground=MUTED)
        self.file_count.pack(anchor='w', pady=(8, 0))

        # ── Right: styles ──
        right = ttk.Frame(body, style='Card.TFrame', padding=12)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(3, weight=1)
        right.rowconfigure(5, weight=1)
        right.columnconfigure(0, weight=1)

        top = ttk.Frame(right, style='Card.TFrame')
        top.grid(row=0, column=0, sticky='ew')
        ttk.Label(top, text="2 · Pick the styles to keep", style='Head.TLabel').pack(side='left')
        for text, cmd in (("Invert", self.invert), ("None", self.select_none),
                          ("All", self.select_all), ("✨ Smart pick", self.smart_pick)):
            ttk.Button(top, text=text, command=cmd, padding=(8, 3)).pack(side='right', padx=(4, 0))
        self.level_var = tk.StringVar(value=LEVEL_LABELS[0])
        lvl = ttk.Frame(right, style='Card.TFrame')
        lvl.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        ttk.Label(lvl, text="Smart level", style='Card.TLabel',
                  foreground=MUTED).pack(side='left', padx=(0, 8))
        seg = tk.Frame(lvl, bg=PANEL_ALT, padx=3, pady=3)
        seg.pack(side='left')
        self.level_btns = []
        for i, label in enumerate(LEVEL_LABELS):
            b = tk.Label(seg, text=label, font=('Segoe UI', 9, 'bold'), padx=14, pady=5,
                         cursor='hand2', bg=PANEL_ALT, fg=MUTED)
            b.pack(side='left', padx=1)
            b.bind('<Button-1>', lambda e, i=i: self._set_level(i))
            b.bind('<Enter>', lambda e, i=i: self._paint_levels(hover=i))
            b.bind('<Leave>', lambda e: self._paint_levels())
            self.level_btns.append(b)
        self.level_desc = ttk.Label(lvl, text="", style='Card.TLabel', foreground=MUTED,
                                    wraplength=330, justify='left')
        self.level_desc.pack(side='left', padx=(14, 0), fill='x', expand=True)
        self._paint_levels()

        self.search = tk.StringVar()
        self.search.trace_add('write', lambda *_: self.refresh_tree() if self.files else None)
        ttk.Entry(right, textvariable=self.search).grid(row=2, column=0, sticky='ew', pady=8)

        tf = ttk.Frame(right, style='Card.TFrame')
        tf.grid(row=3, column=0, sticky='nsew')
        cols = ('use', 'style', 'lines', 'kind', 'sample')
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='browse')
        for c, t, w, a in (('use', '', 40, 'center'), ('style', 'Style', 150, 'w'),
                           ('lines', 'Lines', 60, 'center'), ('kind', 'Detected', 110, 'w'),
                           ('sample', 'Sample text', 300, 'w')):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == 'sample'))
        tsb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tsb.pack(side='right', fill='y')
        for kind, color in KIND_COLORS.items():
            self.tree.tag_configure(kind, foreground=color)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<space>', self._on_space)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show_preview())

        ttk.Label(right, text="Preview of selected style", style='Head.TLabel')\
            .grid(row=4, column=0, sticky='w', pady=(10, 4))
        pf = ttk.Frame(right, style='Card.TFrame')
        pf.grid(row=5, column=0, sticky='nsew')
        self.preview = tk.Text(pf, height=6, bg=PANEL_ALT, fg=FG, relief='flat',
                               highlightthickness=0, font=('Consolas', 9), wrap='word',
                               state='disabled', padx=8, pady=6)
        psb = ttk.Scrollbar(pf, orient='vertical', command=self.preview.yview)
        self.preview.configure(yscrollcommand=psb.set)
        self.preview.pack(side='left', fill='both', expand=True)
        psb.pack(side='right', fill='y')

        # ── Footer ──
        foot = ttk.Frame(outer)
        foot.pack(fill='x', pady=(12, 0))
        ttk.Label(foot, text="Output folder").pack(side='left')
        ttk.Entry(foot, textvariable=self.out_dir).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(foot, text="Browse", command=self.browse_out).pack(side='left')
        self.convert_btn = ttk.Button(foot, text="Convert to SRT", style='Accent.TButton',
                                      command=self.convert, state='disabled')
        self.convert_btn.pack(side='left', padx=(12, 0))

        self.summary = ttk.Label(outer, text="Leave output folder empty to save next to each .ass file",
                                 style='Sub.TLabel')
        self.summary.pack(anchor='w', pady=(6, 0))
        self.progress = ttk.Progressbar(outer, mode='determinate')
        self.progress.pack(fill='x', pady=(6, 0))

    # ─────────── file handling ───────────
    def _on_drop(self, event):
        paths = re.findall(r'\{([^}]*)\}|(\S+)', event.data)
        self._add_paths([a or b for a, b in paths])

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select ASS files", filetypes=[("ASS subtitles", "*.ass *.ssa"), ("All", "*.*")])
        self._add_paths(paths)

    def _add_paths(self, paths):
        errors = []
        for p in paths:
            p = os.path.normpath(p)
            if not p.lower().endswith(('.ass', '.ssa')) or p in self.files:
                continue
            try:
                self.events[p] = parse_events(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
                continue
            self.files.append(p)
        if errors:
            messagebox.showwarning("Could not read", "\n".join(errors))
        self.rebuild()

    def remove_files(self):
        idx = [i for i in self.file_list.curselection() if 1 <= i <= len(self.files)]
        if not idx:
            return
        for i in reversed(idx):
            p = self.files.pop(i - 1)
            self.events.pop(p, None)
        self.rebuild()

    def clear_files(self):
        self.files.clear()
        self.events.clear()
        self.rebuild()

    def browse_out(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.out_dir.set(os.path.normpath(d))

    # ─────────── file list / scope ───────────
    def _scope_paths(self):
        """Files whose styles are shown: the selected files, or all when 'All files' is selected."""
        cur = self.file_list.curselection()
        if 0 in cur:
            return list(self.files)
        sel = [self.files[i - 1] for i in cur if 1 <= i <= len(self.files)]
        return sel or list(self.files)

    def _on_scope_change(self, _=None):
        self.refresh_tree()
        self.show_preview()
        self.update_summary()

    def _on_file_right_click(self, event):
        i = self.file_list.nearest(event.y)
        if i < 1 or i > len(self.files):
            return
        if i not in self.file_list.curselection():
            self.file_list.selection_clear(0, 'end')
            self.file_list.selection_set(i)
            self._on_scope_change()
        self.file_menu.tk_popup(event.x_root, event.y_root)

    # ─────────── style table ───────────
    def rebuild(self):
        cur = self.file_list.curselection()
        all_mode = (not cur) or 0 in cur
        prev = [] if all_mode else self._scope_paths()

        self.file_list.delete(0, 'end')
        self.file_list.insert('end', "★  All files")
        self.file_list.itemconfig(0, fg=ACCENT)
        for p in self.files:
            self.file_list.insert('end', f"  {os.path.basename(p)}")
        if not self.files and HAS_DND:
            self.file_list.insert('end', "")
            self.file_list.insert('end', "     ⬇  Drop .ass files here")
            self.file_list.itemconfig(2, fg=MUTED)
        total = sum(len(v) for v in self.events.values())
        self.file_count.config(
            text=f"{len(self.files)} file(s) • {total} dialogue lines" if self.files
            else "No files loaded")

        # per-file style analysis + tick state (existing ticks are kept)
        self.file_styles = {p: self.file_styles[p] for p in self.files if p in self.file_styles}
        self.checked = {p: self.checked[p] for p in self.files if p in self.checked}
        for p in self.files:
            if p in self.file_styles:
                continue
            fs = {}
            for ev in self.events[p]:
                fs.setdefault(ev['style'], {'events': []})['events'].append(ev)
            smart_levels(fs)
            self.file_styles[p] = fs
            self.checked[p] = {n: d['picks'][self.level] for n, d in fs.items()}

        keep = [self.files.index(p) + 1 for p in prev if p in self.files]
        if keep:
            for i in keep:
                self.file_list.selection_set(i)
        else:
            self.file_list.selection_set(0)
        self._on_scope_change()

    def _build_view(self):
        view = {}
        for p in self.scope:
            for name, d in self.file_styles[p].items():
                v = view.setdefault(name, {'events': [], 'paths': []})
                v['events'].extend(d['events'])
                v['paths'].append(p)
        for name, v in view.items():
            main = max(v['paths'], key=lambda p: len(self.file_styles[p][name]['events']))
            v['kind'] = self.file_styles[main][name]['kinds'][self.level]
        self.view = view

    def _state_glyph(self, name):
        v = self.view[name]
        vals = [self.checked[p][name] for p in v['paths']]
        if all(vals):
            return CHECKED
        return MIXED if any(vals) else UNCHECKED

    def refresh_tree(self):
        self.scope = self._scope_paths()
        self._build_view()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        q = self.search.get().strip().lower()
        for name, d in sorted(self.view.items(), key=lambda kv: -len(kv[1]['events'])):
            sample = next((e['text'].replace('\n', ' ') for e in d['events']
                           if e['text'] and not e['is_drawing']), '')
            if q and q not in name.lower() and q not in sample.lower():
                continue
            self.tree.insert('', 'end', iid=name, tags=(d['kind'],), values=(
                self._state_glyph(name), name, len(d['events']), d['kind'], sample[:90]))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def _toggle(self, name):
        v = self.view[name]
        new = not all(self.checked[p][name] for p in v['paths'])
        for p in v['paths']:
            self.checked[p][name] = new
        self.tree.set(name, 'use', self._state_glyph(name))
        self.update_summary()

    def _on_tree_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) == '#1' \
                and self.tree.identify_region(event.x, event.y) == 'cell':
            self._toggle(row)
            return 'break'

    def _on_space(self, _):
        for row in self.tree.selection():
            self._toggle(row)

    def _set_all(self, fn):
        """Apply fn(path, style) -> bool to every style of the files in view."""
        for p in self.scope:
            for name in self.file_styles[p]:
                self.checked[p][name] = fn(p, name)
        self.refresh_tree()
        self.update_summary()

    def select_all(self):
        self._set_all(lambda p, n: True)

    def select_none(self):
        self._set_all(lambda p, n: False)

    def invert(self):
        self._set_all(lambda p, n: not self.checked[p][n])

    @property
    def level(self):
        return LEVEL_LABELS.index(self.level_var.get()) + 1

    def _paint_levels(self, hover=None):
        cur = self.level - 1
        for i, b in enumerate(self.level_btns):
            if i == cur:
                b.config(bg=ACCENT, fg='white')
            else:
                b.config(bg='#3a3e5a' if i == hover else PANEL_ALT, fg=FG if i == hover else MUTED)
        self.level_desc.config(text=LEVEL_DESC[self.level])

    def _set_level(self, i):
        self.level_var.set(LEVEL_LABELS[i])
        self._paint_levels()
        self.refresh_tree()
        self.smart_pick()

    def smart_pick(self):
        self._set_all(lambda p, n: self.file_styles[p][n]['picks'][self.level])

    def show_preview(self):
        sel = self.tree.selection()
        self.preview.config(state='normal')
        self.preview.delete('1.0', 'end')
        if sel and sel[0] in self.view:
            evs = [e for e in self.view[sel[0]]['events'] if e['text'] and not e['is_drawing']]
            evs.sort(key=lambda e: time_to_ms(e['start']))
            for e in evs[:40]:
                self.preview.insert('end', f"{e['start'][:-1]}  {e['text'].replace(chr(10), ' / ')}\n")
            if len(evs) > 40:
                self.preview.insert('end', f"… and {len(evs) - 40} more")
        self.preview.config(state='disabled')

    def update_summary(self):
        if not self.files:
            self.summary.config(text="Leave output folder empty to save next to each .ass file")
            self.convert_btn.config(state='disabled')
            return
        in_view = [(p, n) for p in self.scope for n in self.file_styles[p]]
        ticked_view = sum(self.checked[p][n] for p, n in in_view)
        count, files_out = 0, 0
        for p in self.files:
            c = sum(1 for e in self.events[p] if self.checked[p].get(e['style'])
                    and e['text'] and not e['is_drawing'])
            count += c
            files_out += bool(c)
        label = "all files" if len(self.scope) == len(self.files) else f"{len(self.scope)} selected file(s)"
        self.summary.config(
            text=f"In view ({label}): {ticked_view} of {len(in_view)} style ticks  •  "
                 f"Output: ~{count} subtitles in {files_out} SRT file(s)")
        self.convert_btn.config(state='normal' if files_out else 'disabled')

    # ─────────── conversion ───────────
    def convert(self):
        out = self.out_dir.get().strip(' "\'')
        if out and not os.path.isdir(out):
            messagebox.showerror("Output folder", "The output folder does not exist.")
            return

        ok, report = 0, []
        self.progress.config(maximum=len(self.files), value=0)
        for i, path in enumerate(self.files, 1):
            chosen = self.checked[path]
            evs = [e for e in self.events[path]
                   if chosen.get(e['style']) and e['text'] and not e['is_drawing']]
            name = os.path.basename(path)
            if not evs:
                report.append(f"⚠ {name}: no matching lines")
            else:
                evs.sort(key=lambda e: time_to_ms(e['start']))
                dest = os.path.join(out or os.path.dirname(os.path.abspath(path)),
                                    os.path.splitext(name)[0] + '.srt')
                try:
                    with open(dest, 'w', encoding='utf-8') as f:
                        for n, e in enumerate(evs, 1):
                            f.write(f"{n}\n{format_time(e['start'])} --> {format_time(e['end'])}\n"
                                    f"{e['text']}\n\n")
                    ok += 1
                except OSError as err:
                    report.append(f"✖ {name}: {err}")
            self.progress.config(value=i)
            self.root.update_idletasks()

        msg = f"✅ {ok} of {len(self.files)} SRT file(s) created."
        if report:
            msg += "\n\n" + "\n".join(report)
        messagebox.showinfo("Done", msg)


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
