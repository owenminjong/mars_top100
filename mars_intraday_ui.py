"""Optional, non-modal same-time comparison window; main table is untouched."""
import queue
import math
import time
import tkinter as tk
from tkinter import ttk

import mars_core as core
from kis_intraday import (OPEN, CLOSE, CompareRequest, Comparison, ComparisonService,
                          GraphRequest, GraphService, graph_points, minute_text, read_minute)


def readable_amount(value, signed=False):
    """Display rounded Korean amount units; calculations keep exact integer KRW."""
    if value is None:
        return "—"
    amount = abs(value)
    prefix = "−" if value < 0 else "+" if signed and value > 0 else ""
    if amount >= 1_000_000_000_000:
        trillion = amount // 1_000_000_000_000
        eok = round((amount % 1_000_000_000_000) / 100_000_000)
        trillion, eok = trillion + eok // 10000, eok % 10000
        text = f"{trillion:,}조" + (f" {eok:,}억" if eok else "")
    elif amount >= 100_000_000:
        text = f"{amount / 100_000_000:,.1f}".rstrip("0").rstrip(".") + "억"
    elif amount >= 10000:
        text = f"{amount / 10000:,.1f}".rstrip("0").rstrip(".") + "만"
    else:
        text = f"{amount:,}"
    return prefix + text + " 원"


def axis_amount(value):
    for unit, suffix in ((1e12, "조"), (1e8, "억"), (1e4, "만")):
        if value >= unit:
            return f"{value / unit:,.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,.0f}원"


class ComparisonWindow(tk.Toplevel):
    def __init__(self, owner, service=None, autostart=True, quote=None, day=None, live=True):
        super().__init__(owner.root)
        self.owner = owner
        self.service = service or ComparisonService(owner.client)
        self.fetch = core.FetchController(self.service)
        self.graph_fetch = core.FetchController(GraphService())
        self.serial, self.session, self.graph_data, self.graph_request = 0, None, None, None
        self.results, self.quotes = {}, ()
        self.sort_key, self.descending = "value_rate", True
        self.closed, self.running, self.timer = False, False, None
        self.active_code = None
        self.auto_enabled, self.auto_key, self.auto_not_before = True, None, 0
        self.preserve_graph = False
        self.title("동시간 거래대금 비교 — 마스 부가기능")
        width = min(1280, max(960, self.winfo_screenwidth() - 60))
        height = min(860, max(580, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(1020, width), min(720, height))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.setup_ui()
        self.timer = self.after(100, self.poll)
        if quote is not None:
            self.select_stock(quote, day, live, autostart)

    def setup_ui(self):
        controls = ttk.Frame(self, padding=(12, 12, 12, 4))
        controls.pack(fill=tk.X)
        self.mode = tk.StringVar(value="latest")
        ttk.Radiobutton(controls, text="최신 · 매분 갱신", variable=self.mode, value="latest",
                        command=self.update_mode).pack(side=tk.LEFT)
        ttk.Radiobutton(controls, text="직접 지정", variable=self.mode, value="manual",
                        command=self.update_mode).pack(side=tk.LEFT, padx=(10, 4))
        now = core.now_kst()
        self.date = tk.StringVar(value=now.date().isoformat())
        self.minute = tk.StringVar(value=minute_text(max(OPEN, min(CLOSE, now.hour * 60 + now.minute - 1))))
        self.date_entry = ttk.Entry(controls, textvariable=self.date, width=12)
        self.date_entry.pack(side=tk.LEFT, padx=4)
        self.time_entry = ttk.Entry(controls, textvariable=self.minute, width=6)
        self.time_entry.pack(side=tk.LEFT)
        self.date_entry.bind("<Return>", lambda _: self.start())
        self.time_entry.bind("<Return>", lambda _: self.start())
        ttk.Label(controls, text="KST · 완료 분봉 시각").pack(side=tk.LEFT, padx=6)
        self.run_button = ttk.Button(controls, text="선택 종목 조회", command=self.start)
        self.run_button.pack(side=tk.LEFT, padx=6)
        self.stop_button = ttk.Button(controls, text="중지", command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT)
        self.update_mode()
        self.scope = ttk.Label(self, padding=(14, 4), wraplength=1200)
        self.scope.pack(fill=tk.X)
        self.status = ttk.Label(self, text="조회 준비", padding=(14, 4), wraplength=1200)
        self.status.pack(fill=tk.X)
        self.period = ttk.Label(self, padding=(14, 4), wraplength=1200)
        self.period.pack(fill=tk.X)
        table = ttk.Frame(self, padding=(12, 5))
        table.pack(fill=tk.X)
        self.columns = {
            "trade_value": ("기준일 동시간 누적", 195), "previous_value": ("전 거래일 동시간 누적", 195),
            "value_change": ("증가액", 180), "value_rate": ("증가율", 120), "state": ("비교 상태", 400)}
        self.tree = ttk.Treeview(table, columns=tuple(self.columns), show="headings", height=1,
                                 selectmode="browse")
        for key, (text, width) in self.columns.items():
            self.tree.heading(key, text=text, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=width, minwidth=50, stretch=False,
                             anchor="w" if key in ("name", "market", "state") else "e")
        self.tree.tag_configure("up", foreground="#b42318")
        self.tree.tag_configure("down", foreground="#245fa5")
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        chart = ttk.Frame(self, padding=(12, 5))
        chart.pack(fill=tk.BOTH, expand=True)
        bar = ttk.Frame(chart, padding=(0, 7))
        bar.pack(fill=tk.X)
        self.chart_title = ttk.Label(bar, text="선택 종목의 누적 거래대금")
        self.chart_title.pack(side=tk.LEFT)
        self.interval = tk.StringVar(value="5분")
        picker = ttk.Combobox(bar, textvariable=self.interval, values=("5분", "15분"), width=5,
                              state="readonly")
        picker.pack(side=tk.RIGHT)
        picker.bind("<<ComboboxSelected>>", lambda _: self.draw_graph())
        ttk.Label(bar, text="표시 간격 ").pack(side=tk.RIGHT)
        self.canvas = tk.Canvas(chart, background="#ffffff", highlightthickness=1,
                                highlightbackground="#d7dde5", height=260)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _: self.draw_graph())
        self.canvas.bind("<Motion>", self.hover_graph)
        self.canvas.bind("<Leave>", lambda _: self.canvas.delete("hover"))
        self.hover = ttk.Label(chart, text=" ", padding=(5, 4))
        self.hover.pack(fill=tk.X)
        ttk.Label(self, padding=(14, 5, 14, 10), wraplength=1220,
                  text="KRX · 09:00~15:30 범위의 같은 완료 분봉끼리 비교합니다. 예: 10:30 조회 → 10:29분까지. "
                       "직접 지정 10:30은 10:30분을 포함합니다.\n"
                       "그래프는 5분/15분마다 누적값을 표시합니다. 분봉이 없으면 비교 불가·선 끊김으로 표시하며, "
                       "전 거래일 0원은 증가율 —입니다. 09:00 분봉이 없는 지연개장일 등도 비교에서 제외합니다.\n"
                       "선택 종목만 조회 · 최신 모드는 매분 갱신 · 초 단위 체결 중계 아님 · 분봉·그래프 파일 저장 없음").pack(fill=tk.X)

    def update_mode(self):
        state = "normal" if self.mode.get() == "manual" else "disabled"
        self.date_entry.configure(state=state)
        self.time_entry.configure(state=state)
        if hasattr(self, "tree"):
            self.stop(silent=True)
            self.auto_enabled = False
            self.status.configure(text="선택 종목 조회를 눌러 적용하세요", foreground="#333333")

    def select_stock(self, quote, day, live, autostart=True):
        self.stop(silent=True)
        self.quotes = (quote,)
        self.results.clear()
        self.session = self.graph_request = self.graph_data = None
        self.active_code = quote.code
        self.title(f"{quote.name} — 동시간 거래대금 비교")
        self.scope.configure(text=f"{quote.name} ({quote.code}) · {core.MARKETS[quote.market]} · "
                             f"메인에서 선택한 날짜 {day} · 이 종목만 조회")
        self.mode.set("latest" if live else "manual")
        self.date.set(day)
        self.minute.set("15:30")
        self.update_mode()
        self.status.configure(text="선택 종목 조회 준비", foreground="#333333")
        self.period.configure(text="")
        self.render()
        self.draw_graph()
        if autostart:
            self.start()

    def start(self, automatic=False):
        if self.closed:
            return
        if self.owner.settings_pending or self.owner.settings_dialog is not None:
            return
        try:
            if not self.quotes:
                raise core.DataError("메인 화면에서 종목을 클릭하세요")
            quote = self.quotes[0]
            date = None if self.mode.get() == "latest" else core.valid_date(self.date.get().strip())
            minute = self.minute.get().strip()
            if date is not None:
                read_minute(minute)
        except core.DataError as exc:
            self.stop(silent=True)
            self.finish_pending("비교 불가 · 입력 확인")
            self.render()
            self.status.configure(text=str(exc), foreground="#b42318")
            return
        self.stop(silent=True)
        self.serial += 1
        self.active_code = quote.code
        self.auto_enabled = True
        now = core.now_kst()
        self.auto_key = (now.date().isoformat(), now.hour * 60 + now.minute)
        if not automatic:
            self.auto_not_before = 0
        self.results.clear()
        self.preserve_graph = automatic and self.graph_data is not None
        if not self.preserve_graph:
            self.session = self.graph_data = None
        self.graph_request = None
        self.running = True
        self.fetch.submit(CompareRequest(self.serial, (quote,), date, minute))
        self.status.configure(text="거래일 확인 중…", foreground="#333333")
        self.period.configure(text="")
        self.stop_button.configure(state=tk.NORMAL)
        self.render()
        self.draw_graph()

    def stop(self, silent=False):
        self.serial += 1
        self.running = False
        self.fetch.stop()
        self.graph_fetch.stop()
        self.graph_request = None
        self.stop_button.configure(state=tk.DISABLED)
        if not silent:
            self.auto_enabled = False
            self.finish_pending("비교 불가 · 조회 중지")
            self.status.configure(text="조회 중지 · 받은 결과 유지", foreground="#333333")
            self.render()

    def finish_pending(self, message):
        if self.active_code:
            self.results.setdefault(self.active_code, Comparison(self.active_code, None, None, message))

    def poll(self):
        if self.closed:
            return
        dirty = False
        while True:
            try:
                event = self.service.progress.get_nowait()
            except queue.Empty:
                break
            if event.serial != self.serial:
                continue
            if event.session:
                self.session = event.session
                moment = self.session.moment
                self.date.set(moment.day)
                self.minute.set(minute_text(moment.minute))
                self.period.configure(text=f"기준일 {moment.day} ↔ 전 거래일 {moment.previous} · "
                                      f"표는 양쪽 {minute_text(moment.minute)}분까지 비교 · 전 거래일 그래프는 장 마감까지 표시 · {moment.note}")
            if event.comparison:
                self.results[event.comparison.code] = event.comparison
                dirty = True
        done = self.fetch.poll()
        if done:
            self.running = False
            self.stop_button.configure(state=tk.DISABLED)
            if done.error:
                self.finish_pending("비교 불가 · 조회 미완료")
                self.status.configure(text=str(done.error), foreground="#b42318")
                self.auto_not_before = time.monotonic() + max(60, getattr(done.error, "retry_after", 0))
            else:
                self.status.configure(text=self.progress_text("조회 완료"), foreground="#237347")
                self.load_graph()
            dirty = True
        elif dirty and self.running:
            self.status.configure(text=self.progress_text("조회 중"), foreground="#333333")
        if dirty:
            self.render()
        graph = self.graph_fetch.poll()
        if graph and graph.selection == self.graph_request:
            if graph.error:
                self.chart_title.configure(text=f"그래프 조회 실패: {graph.error}")
                self.auto_not_before = time.monotonic() + max(60, getattr(graph.error, "retry_after", 0))
            else:
                self.graph_data = graph.result
                name = next((q.name for q in self.quotes if q.code == graph.selection.code), graph.selection.code)
                suffix = " · " + " / ".join(graph.result.errors) if graph.result.errors else ""
                self.chart_title.configure(text=f"{name} · 누적 거래대금{suffix}")
            self.draw_graph()
        self.auto_refresh()
        self.timer = self.after(100, self.poll)

    def progress_text(self, prefix):
        name = next((q.name for q in self.quotes if q.code == self.active_code), "")
        mode = "최신 모드 · 장중 매분 자동 갱신" if self.mode.get() == "latest" else "지정 시점 조회"
        return f"{prefix} · {name} 한 종목 · {mode}"

    def render(self):
        selected = self.tree.selection()
        view = self.tree.yview()
        rows = []
        for rank, quote in enumerate(self.quotes, 1):
            result = self.results.get(quote.code)
            rows.append({"rank": rank, "name": quote.name, "code": quote.code,
                "market": core.MARKETS[quote.market], "trade_value": result.current if result else None,
                "previous_value": result.previous if result else None,
                "value_change": result.difference if result else None, "value_rate": result.rate if result else None,
                "state": result.status if result else "조회 중" if self.running and quote.code == self.active_code else "선택 시 조회"})
        known = sorted((r for r in rows if r[self.sort_key] is not None),
                       key=lambda r: r[self.sort_key], reverse=self.descending)
        missing = [r for r in rows if r[self.sort_key] is None]
        for index, row in enumerate(known + missing):
            change = row["value_change"]
            tag = "up" if change is not None and change > 0 else "down" if change is not None and change < 0 else ""
            values = [readable_amount(row[key], signed=key == "value_change")
                      if key in ("trade_value", "previous_value", "value_change")
                      else core.format_cell(key, row[key]) for key in self.columns]
            if self.tree.exists(row["code"]):
                self.tree.item(row["code"], values=values, tags=(tag,))
                self.tree.move(row["code"], "", index)
            else:
                self.tree.insert("", index, iid=row["code"], values=values, tags=(tag,))
        codes = {q.code for q in self.quotes}
        for code in self.tree.get_children():
            if code not in codes:
                self.tree.delete(code)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if view:
            self.tree.yview_moveto(view[0])
        for key, (title, _) in self.columns.items():
            self.tree.heading(key, text=title + (" ▼" if self.descending else " ▲") if key == self.sort_key else title)

    def sort(self, key):
        self.descending = not self.descending if self.sort_key == key else key in (
            "trade_value", "previous_value", "value_change", "value_rate")
        self.sort_key = key
        self.render()

    def load_graph(self):
        if self.closed or not self.active_code or self.session is None:
            return
        request = GraphRequest(self.session, self.active_code)
        if request == self.graph_request:
            return
        self.graph_request = request
        if not self.preserve_graph:
            self.graph_data = None
        self.graph_fetch.submit(request)
        name = self.quotes[0].name
        self.chart_title.configure(text=f"{name} · 선택 종목의 분봉을 불러오는 중…")
        self.draw_graph()

    def auto_refresh(self):
        """One refresh per newly completed minute, only for the selected stock."""
        if (self.closed or not self.auto_enabled or self.mode.get() != "latest" or not self.active_code
                or self.fetch.busy or self.graph_fetch.busy or time.monotonic() < self.auto_not_before):
            return
        now = core.now_kst()
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not OPEN + 1 <= minute <= CLOSE + 1:
            return
        key = (now.date().isoformat(), minute)
        if key == self.auto_key:
            return
        # A fallback confirmed during market hours is a holiday/no-session signal.
        # Before-open fallback gets one new check at 09:01; do not poll all day on holidays.
        if (self.session and self.session.request.date is None and self.session.moment.day < key[0] and self.auto_key
                and self.auto_key[0] == key[0] and self.auto_key[1] >= OPEN + 1):
            return
        self.start(automatic=True)

    def draw_graph(self):
        canvas = self.canvas
        canvas.delete("all")
        self.chart_points = []
        self.hover.configure(text=" ")
        width, height = max(300, canvas.winfo_width()), max(180, canvas.winfo_height())
        if not self.graph_data or not self.session:
            canvas.create_text(width / 2, height / 2, text="선택 종목의 분봉 자료를 준비합니다",
                               fill="#687585", font=("맑은 고딕", 11))
            return
        moment = self.session.moment
        points = graph_points(self.graph_data, CLOSE, int(self.interval.get()[:-1]), moment.minute)
        values = [v for row in points for v in row[1:] if v is not None]
        if not values:
            canvas.create_text(width / 2, height / 2, text="해당 시간의 분봉 자료가 없어 그래프를 표시할 수 없습니다",
                               fill="#687585")
            return
        left, top, right, bottom = 85, 40, width - 25, height - 40
        target_step = max(max(values) * 1.08 / 4, 1)
        magnitude = 10 ** math.floor(math.log10(target_step))
        step = next(s * magnitude for s in (1, 2, 2.5, 5, 10) if s * magnitude >= target_step)
        maximum = max(step, math.ceil(max(values) / step) * step)
        x = lambda minute: left + (minute - OPEN) / (CLOSE - OPEN) * (right - left)
        y = lambda value: bottom - value / maximum * (bottom - top)
        canvas.create_text(left, 15, anchor="w", text=f"● 기준일 {moment.day}", fill="#b42318")
        canvas.create_text(left + 190, 15, anchor="w", text=f"┄ 전 거래일 {moment.previous}", fill="#245fa5")
        canvas.create_text(right, 15, anchor="e", text="누적 거래대금 · 억/조 단위", fill="#687585")
        for i in range(round(maximum / step) + 1):
            amount = step * i
            canvas.create_line(left, y(amount), right, y(amount), fill="#e8ecf2")
            canvas.create_text(left - 9, y(amount), anchor="e", text=axis_amount(amount), fill="#687585")
        ticks = list(range(OPEN, CLOSE + 1, 30))
        for minute in ticks:
            canvas.create_text(x(minute), bottom + 18, text=minute_text(minute), fill="#687585")
        for side, color in ((1, "#b42318"), (2, "#245fa5")):
            previous = None
            for minute, current, prior in points:
                value = (minute, current, prior)[side]
                if value is None:
                    previous = None
                    continue
                point = (x(minute), y(value))
                if previous:
                    canvas.create_line(*previous, *point, fill=color, width=2,
                                       dash=(5, 3) if side == 2 else ())
                canvas.create_oval(point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2,
                                   fill=color, outline=color)
                previous = point
        self.chart_points = [(x(m), m, a, b) for m, a, b in points]
        self.chart_bounds = (top, bottom)
        canvas.create_line(x(moment.minute), top, x(moment.minute), bottom, fill="#9aa5b4", dash=(3, 3))
        missing = sum((a is None or b is None) and m <= moment.minute for m, a, b in points)
        self.hover.configure(text=f"점 위에 마우스를 올리면 금액과 증가율을 확인합니다. "
                             f"기준일 도달 시각 {minute_text(moment.minute)} · 도달 범위의 결측 {missing}개")

    def hover_graph(self, event):
        if not self.chart_points:
            return
        x, minute, current, previous = min(self.chart_points, key=lambda p: abs(p[0] - event.x))
        self.canvas.delete("hover")
        self.canvas.create_line(x, self.chart_bounds[0], x, self.chart_bounds[1],
                                fill="#687585", dash=(2, 3), tags="hover")
        amount = lambda v: readable_amount(v) if v is not None else "비교 불가"
        difference = current - previous if current is not None and previous is not None else None
        rate = core.percentage(current, previous)
        current_text = "아직 도달하지 않음" if self.session and minute > self.session.moment.minute else amount(current)
        self.hover.configure(text=f"{minute_text(minute)}분 · 기준일 {current_text} · 전 거래일 {amount(previous)} · "
                             f"증가액 {readable_amount(difference, signed=True)} · 증가율 {core.format_cell('value_rate', rate)}")

    def close(self):
        if self.closed:
            return
        self.stop(silent=True)
        self.closed = True
        if self.timer:
            self.after_cancel(self.timer)
        # The main app drains cancelled workers before opening the credential editor.
        self.owner.retired_aux.extend((self.fetch, self.graph_fetch))
        self.owner.aux_window = None
        self.session = self.graph_data = self.graph_request = None
        self.results.clear()
        self.destroy()
