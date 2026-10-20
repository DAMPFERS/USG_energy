"""
Консоль организатора (Textual): состояние купола в реальном времени + командная строка.
"""
from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, RichLog, Static

from .client import AdminClient, ApiError
from .commands import HELP, CommandError, Quit, execute

LINE_STYLE = {"on": ("ВКЛ", "bold green"), "off": ("выкл", "grey62"), "locked": ("БЛОК", "bold yellow"),
              "tripped": ("АВАРИЯ", "bold red"), "offline": ("НЕТ СВЯЗИ", "bold magenta")}
BAT_STATUS = {"ok": "норма", "degraded": "деградация", "fault": "защита", "failed": "отказ"}
PHASE_STATE = {"paused": "ПАУЗА", "cancelled": "ОТМЕНЕНА"}
BAT_STYLE = {"ok": "green", "degraded": "yellow", "fault": "bold red", "failed": "bold red"}
MSG_STYLE = {"INFO": "cyan", "WARN": "yellow", "CRIT": "bold red", "OK": "green"}
STATUS_TXT = {"before": "ДО НАЧАЛА", "running": "ИДЁТ", "finished": "ЗАВЕРШЁН"}


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,f1,q", "app.pop_screen", "Закрыть")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 110; height: auto; max-height: 90%; border: round $accent; background: $panel; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        t = Text()
        for line in HELP.splitlines():
            t.append(line + "\n", style="" if line.startswith(" ") else "bold")
        t.append("\nEsc — закрыть", style="dim")
        with VerticalScroll(id="help"):
            yield Static(t)


class CommandInput(Input):
    """Строка команд с историей (стрелки вверх/вниз)."""

    BINDINGS = [Binding("up", "prev", show=False), Binding("down", "next", show=False)]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.history: list[str] = []
        self.pos = 0

    def remember(self, cmd: str):
        if cmd and (not self.history or self.history[-1] != cmd):
            self.history.append(cmd)
        self.pos = len(self.history)

    def action_prev(self):
        if self.history and self.pos > 0:
            self.pos -= 1
            self.value = self.history[self.pos]
            self.cursor_position = len(self.value)

    def action_next(self):
        if self.pos < len(self.history) - 1:
            self.pos += 1
            self.value = self.history[self.pos]
        else:
            self.pos = len(self.history)
            self.value = ""
        self.cursor_position = len(self.value)


class AdminConsole(App):
    TITLE = "Энергосистема купола · консоль организатора"
    CSS = """
    Screen { layout: vertical; }
    #top { height: 1; padding: 0 1; background: $primary-darken-2; }
    #main { height: 1fr; }
    #lines { width: 1fr; height: 100%; border: round $primary; }
    #right { width: 1fr; }
    #power { height: auto; border: round $primary; padding: 0 1; }
    #crisis { height: auto; border: round $warning; padding: 0 1; }
    #bats { height: 1fr; border: round $primary; }
    #bottom { height: 14; }
    #term { width: 1fr; border: round $secondary; }
    #out { width: 1fr; border: round $secondary; }
    #cmd { dock: bottom; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Выход"), Binding("f1", "help", "Справка")]

    def __init__(self, api: AdminClient, interval: float = 1.0):
        super().__init__()
        self.api = api
        self.interval = interval
        self.data: dict | None = None
        self.last_n = 0
        self.first = True
        self.online = False

    def compose(self) -> ComposeResult:
        yield Static("подключение…", id="top")
        with Horizontal(id="main"):
            yield DataTable(id="lines", cursor_type="none", zebra_stripes=True)
            with Vertical(id="right"):
                yield Static(id="power")
                yield Static(id="crisis")
                yield DataTable(id="bats", cursor_type="none")
        with Horizontal(id="bottom"):
            yield RichLog(id="term", wrap=True, markup=False, max_lines=500, min_width=20)
            yield RichLog(id="out", wrap=True, markup=True, max_lines=500, min_width=20)
        yield CommandInput(id="cmd", placeholder="команда (справка — F1) …")

    def on_mount(self):
        self.query_one("#lines", DataTable).add_columns("#", "Ключ", "Потребитель", "Состояние", "кВт", "Блокировка / причина")
        self.query_one("#bats", DataTable).add_columns("ID", "Название", "Статус", "кВт·ч", "Заряд", "кВт",
                                                       "зар/разр/пик", "КПД")
        self.query_one("#lines").border_title = "Линии"
        self.query_one("#power").border_title = "Мощность"
        self.query_one("#crisis").border_title = "Кризис"
        self.query_one("#bats").border_title = "Аккумуляторы"
        self.query_one("#term").border_title = "Терминал участников"
        self.query_one("#out").border_title = "Консоль"
        self.out(f"Сервер: {self.api.url}. Справка по командам — [b]F1[/b].")
        self.query_one("#cmd").focus()
        self.poll()
        self.set_interval(self.interval, self.poll)

    # ------------------------------------------------------------------ вывод
    def out(self, line: str):
        self.query_one("#out", RichLog).write(Text.from_markup(line))

    def action_help(self):
        self.push_screen(HelpScreen())

    # ------------------------------------------------------------------ опрос
    @work(thread=True, exclusive=True, group="poll")
    def poll(self):
        try:
            d = self.api.state(self.last_n)
        except ApiError as e:
            self.call_from_thread(self.show_offline, str(e))
            return
        self.call_from_thread(self.show, d)

    def show_offline(self, err: str):
        if self.online or self.first:
            self.out(f"[red]{escape(err)}[/red]")
        self.online = False
        self.first = False
        self.query_one("#top", Static).update(Text(f" НЕТ СВЯЗИ с {self.api.url} — {err}", style="bold white on red"))

    def show(self, d: dict):
        if not self.online and not self.first:
            self.out("[green]Связь восстановлена[/green]")
        self.online = True
        self.data = d
        s, a = d["state"], d["admin"]
        if not s:
            return
        self.render_top(s, a)
        self.render_lines(s)
        self.render_power(s, a)
        self.render_crisis(s, a)
        self.render_bats(s)
        msgs = d["messages"][-60:] if self.first else d["messages"]
        term = self.query_one("#term", RichLog)
        for m in msgs:
            t = Text()
            t.append(m["ts"][11:], style="grey50")
            t.append(f" {m['level']:<4} ", style=MSG_STYLE.get(m["level"], ""))
            t.append(m["text"])
            term.write(t)
        if d["messages"]:
            self.last_n = d["messages"][-1]["n"]
        self.first = False

    def render_top(self, s, a):
        t = Text(" ")
        t.append(s["now"], style="bold")
        if s.get("speed", 1) != 1:
            t.append(f" ×{s['speed']:g}", style="yellow")
        t.append(f"   {s['day']}   ")
        st = s["status"]
        t.append(STATUS_TXT.get(st, st), style="bold green" if st == "running" else "bold yellow")
        ph = a["phase"]
        if ph["name"]:
            t.append(f"   Фаза: {ph['name']} (до {ph['to']})")
            if ph["state"] != "active":
                t.append(f" [{PHASE_STATE.get(ph['state'], ph['state'])}]", style="bold black on yellow")
        elif st == "running":
            t.append("   Фаза: — (по умолчанию)")
        if a["crisis"]["paused"]:
            t.append("   НАРУШЕНИЯ НА ПАУЗЕ", style="bold black on yellow")
        self.query_one("#top", Static).update(t)

    def render_lines(self, s):
        tbl = self.query_one("#lines", DataTable)
        tbl.clear()
        for l in s["lines"]:
            label, style = LINE_STYLE.get(l["status"], (l["status"], ""))
            if l["status"] in ("locked", "tripped") and l["on"]:
                label += " (вкл!)"
            lock = l["reason"] + (f" · до {l['until']}" if l["until"] else "") if l["reason"] else ""
            tbl.add_row(str(l["id"]), l.get("key") or "-", l["name"], Text(label, style=style),
                        Text(f"{l['kw']:.2f}" if l["on"] else "—", justify="right"),
                        Text(lock, style="yellow" if l["status"] == "locked" else "red"))

    def render_power(self, s, a):
        c, ov = s["corridor"], a["corridor_override"]
        cons = s["cons"]
        bad = cons > c["max"] or cons < c["min"]
        t = Text()
        t.append("Генерация    ")
        t.append(f"{s['gen']:6.2f} кВт", style="bold #ff8c3a")
        t.append(f"   солнце ×{a['solar_factor']:.2f} = фаза {a['solar_phase']:.2f} × админ {a['solar_admin']:.2f}\n",
                 style="grey62" if a["solar_admin"] == 1 else "yellow")
        t.append("Потребление  ")
        t.append(f"{cons:6.2f} кВт", style="bold red" if bad else "bold #5fa8ff")
        t.append("\nБаланс       ")
        t.append(f"{s['net']:+6.2f} кВт", style="green" if s["net"] >= 0 else "red")
        if s["deficit"] > 0.005:
            t.append(f"   ДЕФИЦИТ {s['deficit']:.2f} кВт", style="bold red")
        t.append("\nКоридор      ")
        t.append(f"{c['min']:.2f}", style="bold yellow" if ov["min"] else "bold")
        t.append(" – ")
        t.append(f"{c['max']:.2f}", style="bold yellow" if ov["max"] else "bold")
        t.append(" кВт")
        for b in ("min", "max"):
            o = ov[b]
            if o:
                t.append(f"\n  переопределён {b} {o['value']:.2f} {'до ' + o['until'] if o['until'] else 'до reset'}"
                         f" (по сценарию {a['corridor_base'][b]:.2f})", style="yellow")
        self.query_one("#power", Static).update(t)

    def render_crisis(self, s, a):
        cr = a["crisis"]
        t = Text()
        t.append("Нарушения: ")
        t.append("НА ПАУЗЕ" if cr["paused"] else "отслеживаются",
                 style="bold black on yellow" if cr["paused"] else "green")
        titles = {"overload": "перегруз", "underload": "гибернация", "deficit": "дефицит"}
        for k, title in titles.items():
            tm = cr["timers"][k]
            if tm["elapsed_s"] > 0:
                t.append(f"\n  ⚠ {title}: {tm['elapsed_s']:.0f}/{tm['grace_s']:.0f} с (игровых)", style="bold red")
            if k in cr["penalties"]:
                t.append(f"\n  ✖ {title}: последствие действует до {cr['penalties'][k]}", style="red")
        st = s["stats"]
        t.append(f"\n  всего: перегруз {st['overload']} · гибернация {st['underload']} · дефицит {st['deficit']}",
                 style="grey62")
        self.query_one("#crisis", Static).update(t)

    def render_bats(self, s):
        tbl = self.query_one("#bats", DataTable)
        tbl.clear()
        for b in s["batteries"]:
            active = b["id"] == s["active_battery"]
            st = BAT_STATUS.get(b["status"], b["status"]) + (f" →{b['fault_until'][:5]}" if b.get("fault_until") else "")
            p = b["power_kw"]
            tbl.add_row(Text(("▶" if active else " ") + b["id"], style="bold green" if active else ""), b["name"][:14],
                        Text(st, style=BAT_STYLE.get(b["status"], "")),
                        f"{b['charge_kwh']:.2f}/{b['capacity_kwh']:.1f}",
                        Text(f"{b['soc'] * 100:.0f}%", style="red" if b["soc"] < 0.2 else ""),
                        Text(f"{p:+.2f}", style="green" if p > 0.01 else "red" if p < -0.01 else "grey62"),
                        f"{b['max_charge_kw']:g}/{b['max_discharge_kw']:g}/{b['peak_discharge_kw']:g}",
                        f"{b['efficiency']:g}")

    # ------------------------------------------------------------------ команды
    def on_input_submitted(self, ev: Input.Submitted):
        cmd = ev.value.strip()
        inp = self.query_one("#cmd", CommandInput)
        inp.remember(cmd)
        inp.value = ""
        if cmd:
            self.run_command(cmd)

    def run_command(self, cmd: str):
        self.out(f"[b cyan]> {escape(cmd)}[/b cyan]")
        self._exec(cmd)

    @work(thread=True, group="cmd")
    def _exec(self, cmd: str):
        try:
            lines = execute(cmd, self.api, self.data)
        except Quit:
            self.call_from_thread(self.exit)
            return
        except (CommandError, ApiError) as e:
            lines = [f"[red]{escape(str(e))}[/red]"]
        for l in lines:
            self.call_from_thread(self.out, l)
        self.call_from_thread(self.poll)
