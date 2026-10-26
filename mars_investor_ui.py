"""선택 종목의 외국인·기관 수급 창. 메인 표와 기존 비교 창은 건드리지 않는다."""
import time
import tkinter as tk
from tkinter import ttk

import mars_core as core
from kis_investor import FlowRequest, FlowService, latest_estimate
from mars_intraday_ui import readable_amount

REFRESH_SECONDS = 60       # 장중·확정 대기 중 자동 갱신 간격 (호출 2~3회/분)
LAST_POLL = 21 * 60 + 30   # 이후에는 자동 갱신을 멈추고 새로고침 버튼만 사용
LABELS = ("오늘", "전일", "전전일")


def qty_text(value):
    return "—" if value is None else f"{value:+,}"


def plain_qty(value):
    return "—" if value is None else f"{value:,}"


def amount_text(value, signed=False):
    return "—" if value is None else readable_amount(value, signed=signed)


class InvestorWindow(tk.Toplevel):
    def __init__(self, owner, service=None, quote=None, autostart=True):
        super().__init__(owner.root)
        self.owner = owner
        self.service = service or FlowService(owner.client)
        self.fetch = core.FetchController(self.service)
        self.serial = 0
        self.code = self.name = None
        self.result = None
        self.closed = self.running = False
        self.last_start = self.not_before = 0.0
        self.timer = None
        self.title("외국인·기관 수급 — 마스 부가기능")
        width = min(1500, max(1100, self.winfo_screenwidth() - 60))
        height = min(720, max(560, self.winfo_screenheight() - 120))
        self.geometry(f"{width}x{height}")
        self.minsize(min(1000, width), min(520, height))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.setup_ui()
        self.timer = self.after(200, self.poll)
        if quote is not None:
            self.select_stock(quote, autostart)

    def setup_ui(self):
        controls = ttk.Frame(self, padding=(12, 12, 12, 4))
        controls.pack(fill=tk.X)
        self.stock_label = ttk.Label(controls, text="종목을 선택하세요", font=("맑은 고딕", 11, "bold"))
        self.stock_label.pack(side=tk.LEFT)
        self.refresh_button = ttk.Button(controls, text="새로고침", command=self.start)
        self.refresh_button.pack(side=tk.LEFT, padx=12)
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="자동 갱신(1분)", variable=self.auto).pack(side=tk.LEFT)
        self.status = ttk.Label(self, text="조회 준비", padding=(14, 4), wraplength=1300)
        self.status.pack(fill=tk.X)
        self.notes = ttk.Label(self, padding=(14, 0, 14, 4), wraplength=1300, foreground="#b54708")
        self.notes.pack(fill=tk.X)

        live = ttk.LabelFrame(self, text="오늘 장중 가집계 (증권사 집계 · 순매수 수량 · 단위 주)", padding=8)
        live.pack(fill=tk.X, padx=12, pady=(4, 6))
        self.est_cols = {"time": ("입력 시각", 110), "foreign": ("외국인 순매수(주)", 160),
                         "institution": ("기관 순매수(주)", 160), "total": ("합산(주)", 160)}
        self.est_tree = ttk.Treeview(live, columns=tuple(self.est_cols), show="headings", height=5)
        for key, (title, width) in self.est_cols.items():
            self.est_tree.heading(key, text=title)
            self.est_tree.column(key, width=width, minwidth=60, stretch=False, anchor="w" if key == "time" else "e")
        self.est_tree.grid(row=0, column=0, sticky="w")
        self.est_summary = ttk.Label(live, padding=(14, 0), wraplength=700, justify=tk.LEFT)
        self.est_summary.grid(row=0, column=1, sticky="nw")
        self.est_tree.tag_configure("up", foreground="#b42318")
        self.est_tree.tag_configure("down", foreground="#245fa5")

        daily = ttk.LabelFrame(self, text="일별 수급 (장 종료 후 확정 · 금액은 억/조 표기)", padding=8)
        daily.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
        self.columns = {
            "day": ("날짜", 130), "close": ("종가(원)", 80),
            "f_buy": ("외국인 매수", 105), "f_sell": ("외국인 매도", 105),
            "f_net": ("외국인 순매수", 115), "f_qty": ("외국인 순매수(주)", 125),
            "o_buy": ("기관 매수", 105), "o_sell": ("기관 매도", 105),
            "o_net": ("기관 순매수", 115), "o_qty": ("기관 순매수(주)", 125),
            "p_net": ("개인 순매수", 115), "sum": ("외국인+기관 순매수", 135), "state": ("상태", 190)}
        self.tree = ttk.Treeview(daily, columns=tuple(self.columns), show="headings", selectmode="browse")
        for key, (title, width) in self.columns.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=50, stretch=False,
                             anchor="w" if key in ("day", "state") else "e")
        vertical = ttk.Scrollbar(daily, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(daily, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        daily.rowconfigure(0, weight=1)
        daily.columnconfigure(0, weight=1)
        self.tree.tag_configure("up", foreground="#b42318")
        self.tree.tag_configure("down", foreground="#245fa5")
        self.tree.tag_configure("pending", foreground="#687585")
        ttk.Label(self, padding=(14, 2, 14, 10), wraplength=1300,
                  text="순매수 = 매수 − 매도 (양수 매수 우위, 음수 매도 우위). 외국인은 등록+비등록 외국인을 합친 값입니다. "
                       "확정 수급은 KIS가 장 종료 후 제공하므로 장중에는 오늘 행이 '집계 전'으로 보이고, 제공되면 자동으로 채워집니다. "
                       "장중 가집계는 증권사가 입력한 수량 누계(외국인 09:30·11:20·13:20·14:30, 기관 10:00·11:20·13:20·14:30)로 "
                       "체결 단위 실시간 자료가 아니며 확정값과 다를 수 있습니다. 받은 자료는 저장하지 않습니다.").pack(fill=tk.X)

    def select_stock(self, quote, autostart=True):
        self.fetch.stop()
        self.running = False
        self.code, self.name = quote.code, quote.name
        self.result = None
        self.not_before = 0.0
        self.title(f"{quote.name} — 외국인·기관 수급")
        self.stock_label.configure(text=f"{quote.name} ({quote.code}) · {core.MARKETS.get(quote.market, quote.market)}")
        self.notes.configure(text="")
        self.render()
        if autostart:
            self.start()

    def start(self, automatic=False):
        if self.closed or not self.code:
            return
        if self.owner.settings_pending or self.owner.settings_dialog is not None:
            return
        self.serial += 1
        self.running = True
        self.last_start = time.monotonic()
        if not automatic:
            self.not_before = 0.0
        self.refresh_button.configure(state=tk.DISABLED)
        self.fetch.submit(FlowRequest(self.serial, self.code, self.name))
        self.status.configure(text=f"{self.name} 수급 조회 중…", foreground="#333333")

    def poll(self):
        if self.closed:
            return
        done = self.fetch.poll()
        if done is not None and done.selection.serial == self.serial and not isinstance(done.error, core.Cancelled):
            self.running = False
            self.refresh_button.configure(state=tk.NORMAL)
            if done.error:
                delay = max(60, getattr(done.error, "retry_after", 0))
                self.not_before = time.monotonic() + delay
                suffix = " · 이전 수신 표 유지" if self.result else ""
                self.status.configure(text=f"조회 실패: {done.error}{suffix}", foreground="#b42318")
            else:
                self.result = done.result
                if self.result.retry_after:
                    self.not_before = time.monotonic() + self.result.retry_after
                self.render()
        self.auto_refresh()
        self.timer = self.after(200, self.poll)

    def auto_refresh(self):
        """장중과 확정 대기 동안만 1분마다 갱신한다. 확정 수급을 받으면 멈춘다."""
        if (self.closed or not self.auto.get() or self.running or not self.code or self.result is None
                or time.monotonic() < self.not_before or time.monotonic() - self.last_start < REFRESH_SECONDS):
            return
        now = core.now_kst()
        minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or minute > LAST_POLL:
            return
        if self.result.live and not self.result.today_final:
            self.start(automatic=True)
        elif not self.result.live and 9 * 60 <= minute <= 9 * 60 + 35:
            self.start(automatic=True)  # 개장 직후 새 거래일을 확인한다

    def phase_text(self):
        result = self.result
        now = core.now_kst()
        minute = now.hour * 60 + now.minute
        if not result.live:
            return f"오늘 거래 기록 없음 · 최근 거래일 {result.session_day} 기준"
        if result.today_final:
            return "오늘 확정 수급 반영 완료 · 자동 갱신 종료"
        if minute < 15 * 60 + 30:
            return "장중 · 가집계는 입력 시각마다 갱신되고 확정 수급은 장 종료 후 제공됩니다"
        return "장 종료 · KIS 확정 수급 제공을 기다리는 중 (1분마다 자동 확인)"

    def render(self):
        self.render_estimates()
        self.render_days()
        result = self.result
        if result is None:
            return
        self.status.configure(text=f"{result.name} · 거래일 {result.session_day} · 직전 거래일 {result.previous_day} · "
                              f"{self.phase_text()} · 수신 {result.received_at:%H:%M:%S} KST", foreground="#237347")
        self.notes.configure(text=" / ".join(result.notes))

    def render_estimates(self):
        self.est_tree.delete(*self.est_tree.get_children())
        result = self.result
        if result is None:
            self.est_summary.configure(text="")
            return
        if not result.estimates:
            reason = ("오늘은 장중 가집계가 없습니다" if not result.live else
                      "첫 입력 시각(09:30) 전이거나 아직 입력되지 않았습니다")
            self.est_tree.insert("", tk.END, values=(reason, "", "", ""), tags=())
            self.est_summary.configure(text="")
            return
        for item in result.estimates:
            tag = "up" if (item.total or 0) > 0 else "down" if (item.total or 0) < 0 else ""
            self.est_tree.insert("", tk.END, values=(item.label, qty_text(item.foreign),
                                                     qty_text(item.institution), qty_text(item.total)), tags=(tag,))
        foreign, institution = latest_estimate(result.estimates, "foreign"), latest_estimate(result.estimates, "institution")
        parts = []
        for name, found in (("외국인", foreign), ("기관", institution)):
            parts.append(f"{name} {found[1]:+,}주 ({found[0].label} 입력)" if found else f"{name} 입력 전")
        self.est_summary.configure(text="최근 입력: " + " · ".join(parts) + "\n빈 칸이나 0은 아직 입력 전일 수 있습니다.")

    def render_days(self):
        self.tree.delete(*self.tree.get_children())
        result = self.result
        if result is None:
            return
        flows = {d.day: d for d in result.days}
        order = sorted(set(flows) | {result.session_day}, reverse=True)
        for index, day in enumerate(order):
            label = ("오늘" if result.live else "최근 거래일") if index == 0 else LABELS[index] if index < 3 else ""
            flow = flows.get(day)
            title = f"{day} ({label})" if label else day
            if flow is None:
                state = "집계 전 · 장 종료 후 제공" if result.live and day == result.session_day else "KIS 자료 없음"
                self.tree.insert("", tk.END, iid=day, values=(title,) + ("—",) * (len(self.columns) - 2) + (state,),
                                 tags=("pending",))
                continue
            f, o, p = flow.foreign, flow.institution, flow.person
            state = "확정" if flow.confirmed else "집계 전 · 장 종료 후 제공" if day == result.session_day and result.live else "일부 자료 없음"
            combined = flow.combined_amount
            tag = "pending" if not flow.confirmed else "up" if (combined or 0) > 0 else "down" if (combined or 0) < 0 else ""
            values = (title, plain_qty(flow.close),
                      amount_text(f.buy_amount), amount_text(f.sell_amount), amount_text(f.net_amount, True), qty_text(f.net_qty),
                      amount_text(o.buy_amount), amount_text(o.sell_amount), amount_text(o.net_amount, True), qty_text(o.net_qty),
                      amount_text(p.net_amount, True), amount_text(combined, True), state)
            self.tree.insert("", tk.END, iid=day, values=values, tags=(tag,))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.fetch.stop()
        if self.timer:
            self.after_cancel(self.timer)
        # 메인 앱이 작업자를 모두 정리한 뒤에야 설정 창을 연다(비교 창과 같은 방식).
        self.owner.retired_aux.append(self.fetch)
        self.owner.flow_window = None
        self.result = None
        self.destroy()
