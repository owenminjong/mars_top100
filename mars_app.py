# -*- coding: utf-8 -*-
"""코스피·코스닥·전체의 날짜별 거래대금 TOP100과 직전 거래일 대비 증감."""
import datetime as dt
import logging
import math
from logging.handlers import RotatingFileHandler
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox

import mars_core as core
from kis_client import KISClient
from kis_storage import SecureStore, InstanceLock, purge_legacy_data, validate_password
from kis_provider import KISProvider
from kis_overseas import US_MARKETS, active_window, eastern_now, format_usd, weekend as us_weekend
from mars_intraday_ui import ComparisonWindow
from mars_investor_ui import InvestorWindow

US_ACTIVE_REFRESH_SECONDS = 2
STARTUP_BLOG_URL = "https://blog.naver.com/masgod/224454300727"


class StockApp:
    def __init__(self, root, live_service=None, reference_service=None, autostart=True, client=None):
        self.root = root
        self.client = client or KISClient()
        if live_service is None:
            cache = core.DailyCache(self.client.store.directory)
            live_service = core.LiveService(KISProvider(self.client, cache), cache)
        self.live_service = live_service
        self.reference_service = reference_service or core.ReferenceService(
            live_service.cache, live_service.provider.history, live_service.provider.sectors)
        self.settings_pending = False
        self.lock_pending = False
        self.settings_dialog = None
        self.aux_window = None
        self.flow_window = None
        self.retired_aux = []
        # 전거래일 일봉 수집이 오늘의 미국 자동 갱신을 막지 않도록 두 작업만 분리한다.
        self.live = core.FetchController(self.live_service)
        self.history = core.FetchController(self.reference_service)
        self.selection = core.Selection(None,"value","KOSPI")
        self.snapshot = None
        self.references = {}
        self.view_cache = {}  # 탭·날짜별 마지막 화면(메모리). 다시 돌아오면 바로 표시한다.
        self.closed = self.stopped = False
        self.failures = 0
        self.historical_retry_needed = False
        self.historical_retry_at = 0.0
        self.history_not_before = 0
        self.last_history_request = None
        self.sort_key, self.sort_reverse = "rank", False
        self.auto_timer = self.poll_timer = None
        self.index_events = queue.Queue()
        self.index_cancel = threading.Event()
        self.index_timer = None
        root.title("마스 — 국내·미국주식 순위")
        root.geometry("1580x800")
        root.minsize(1050,500)
        root.protocol("WM_DELETE_WINDOW",self.close)
        self.setup_ui()
        self.update_kst_clock()
        threading.Thread(target=self.market_index_worker, name="market-indexes", daemon=True).start()
        self.poll_timer = root.after(100,self.poll)
        if autostart:
            if self.client.configured:
                self.refresh()
            else:
                root.after(0,self.open_settings)

    def setup_ui(self):
        """시장 탭 3개와 날짜 선택, 결과 표만 둔다. 날짜 선택은 모든 탭에 공통이다."""
        controls = ttk.Frame(self.root,padding=10)
        controls.pack(fill=tk.X)
        ttk.Label(controls,text="조회 날짜").pack(side=tk.LEFT)
        self.date_var = tk.StringVar(value=core.now_kst().date().isoformat())
        entry = ttk.Entry(controls,textvariable=self.date_var,width=13)
        entry.pack(side=tk.LEFT,padx=6)
        entry.bind("<Return>",lambda _event:self.apply_selection())
        ttk.Button(controls,text="오늘",command=lambda:self.choose_day(0)).pack(side=tk.LEFT)
        ttk.Button(controls,text="어제",command=lambda:self.choose_day(1)).pack(side=tk.LEFT,padx=4)
        ttk.Button(controls,text="조회",command=self.apply_selection).pack(side=tk.LEFT)
        self.stop_button = ttk.Button(controls,text="중지",command=self.stop,state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT,padx=4)
        ttk.Button(controls,text="외국인·기관 수급",command=self.open_flow).pack(side=tk.LEFT,padx=4)
        self.mode_var = tk.StringVar(value="거래대금")
        self.mode_combo = ttk.Combobox(controls,textvariable=self.mode_var,values=("거래대금","거래량"),state="readonly",width=8)
        self.mode_combo.pack(side=tk.LEFT,padx=6)
        self.mode_combo.bind("<<ComboboxSelected>>",lambda _event:self.apply_selection())
        ttk.Label(controls,text="미국 프리·정규·애프터장: 조회 완료 후 약 2초 뒤 자동 갱신 · KIS 응답에 따라 지연 가능").pack(side=tk.LEFT,padx=8)
        self.next_label = ttk.Label(controls)
        self.next_label.pack(side=tk.LEFT)
        ttk.Button(controls,text="KIS 설정",command=self.open_settings).pack(side=tk.RIGHT)
        ttk.Button(controls,text="키 잠금",command=lambda:self.open_settings(lock_after=True)).pack(side=tk.RIGHT,padx=4)
        self.tabs = ttk.Notebook(self.root,height=1)
        self.tabs.pack(fill=tk.X,padx=10)
        self.tab_markets = ("KOSPI","KOSDAQ","ALL","US","US_SURGE")
        for title in ("코스피 TOP100","코스닥 TOP100","전체 TOP100","미국 TOP100","미국 급등주"):
            self.tabs.add(ttk.Frame(self.tabs),text=title)
        self.tabs.bind("<<NotebookTabChanged>>",self.tab_changed)
        info = ttk.Frame(self.root,padding=(6,3))
        info.pack(fill=tk.X)
        left = ttk.Frame(info)
        left.pack(side=tk.LEFT,fill=tk.X,expand=True)
        self.status_label = ttk.Label(left,text="조회 준비",padding=(4,4),wraplength=980)
        self.status_label.pack(fill=tk.X,anchor="w")
        self.detail_label = ttk.Label(left,padding=(4,3),wraplength=980)
        self.detail_label.pack(fill=tk.X,anchor="w")
        self.reference_label = ttk.Label(left,padding=(4,3),wraplength=980)
        self.reference_label.pack(fill=tk.X,anchor="w")
        indices = ttk.LabelFrame(info,text="시장 지수 · 한국 표준시간",padding=(8,4))
        indices.pack(side=tk.RIGHT,fill=tk.Y,padx=(8,0))
        self.index_vars = {key:tk.StringVar(value="연결 대기") for key in ("ndx","kospi","kosdaq","kst")}
        for row, (key, title) in enumerate((("ndx","NASDAQ 100 (USD)"),("kospi","KOSPI"),("kosdaq","KOSDAQ"),("kst","한국 표준시간"))):
            ttk.Label(indices,text=title,width=18).grid(row=row,column=0,sticky="w",padx=(0,8),pady=1)
            ttk.Label(indices,textvariable=self.index_vars[key],width=27,anchor="e").grid(row=row,column=1,sticky="e",pady=1)
        self.columns = {
            "rank":("순위",45),"name":("종목명",140),"sector":("업종(현재 기준)",160),"code":("코드",65),"market":("시장",65),
            "trade_value":("거래대금",135),"previous_value":("전거래일 거래대금",135),
            "value_change":("거래대금 증감액",135),"value_rate":("거래대금 증감률",115),
            "open_price":("시가(원)",90),"price":("현재가(원)",95),"change_rate":("등락률",80),
            "volume":("거래량(주)",110),"volume_change":("거래량 증감(주)",120),
            "volume_rate":("거래량 증감률",110),"state":("비교 상태",150)}
        frame = ttk.Frame(self.root,padding=10)
        frame.pack(fill=tk.BOTH,expand=True)
        self.tree = ttk.Treeview(frame,columns=tuple(self.columns),show="headings")
        self.tree.bind("<ButtonRelease-1>",self.open_clicked_comparison)
        for key,(label,width) in self.columns.items():
            self.tree.heading(key,text=label,command=lambda k=key:self.sort_column(k))
            self.tree.column(key,width=width,minwidth=45,stretch=False,
                             anchor="w" if key in ("name","sector","state","market") else "e")
        vertical = ttk.Scrollbar(frame,orient=tk.VERTICAL,command=self.tree.yview)
        horizontal = ttk.Scrollbar(frame,orient=tk.HORIZONTAL,command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.tree.grid(row=0,column=0,sticky="nsew")
        vertical.grid(row=0,column=1,sticky="ns")
        horizontal.grid(row=1,column=0,sticky="ew")
        frame.rowconfigure(0,weight=1)
        frame.columnconfigure(0,weight=1)
        self.tree.tag_configure("up",foreground="#b42318")
        self.tree.tag_configure("down",foreground="#245fa5")
        self.apply_columns()
        ttk.Label(self.root,padding=10,wraplength=1500,
                  text="KRX 기준 · 거래대금·거래량 증감은 같은 종목의 직전 거래일 하루 전체 값과 비교합니다. "
                       "업종은 현재 KIS 분류입니다. 전체 종목을 순차 조회하므로 수신 시점에 차이가 있습니다. 오늘 시세는 저장하지 않습니다. "
                       "종목을 클릭하면 동시간 분봉 비교가 열립니다. "
                       "미국 탭은 나스닥·뉴욕·아멕스 합산 순위이며 급등주는 장중 상승률 기준으로 자동 갱신합니다. 날짜 선택·전 거래일 비교·분봉·수급은 국내 전용입니다.").pack(fill=tk.X)

    def open_clicked_comparison(self,event):
        if self.tree.identify_region(event.x,event.y) not in ("cell","tree"):
            return
        code = self.tree.identify_row(event.y)
        self.open_comparison(code)

    def open_comparison(self,code):
        if self.selection.market in ("US","US_SURGE"):
            return
        if self.settings_pending or self.settings_dialog is not None:
            return
        if not self.snapshot:
            return
        quote = next((q for q in self.snapshot.quotes if q.code == code),None)
        if quote is None:
            return
        if self.flow_window is not None:
            self.flow_window.select_stock(quote)
        day = self.snapshot.data_date
        live = day == core.now_kst().date().isoformat()
        if self.aux_window is not None:
            self.aux_window.select_stock(quote,day,live)
            self.aux_window.lift()
            return
        self.aux_window = ComparisonWindow(self,quote=quote,day=day,live=live)

    def open_flow(self):
        """표에서 선택한 종목의 외국인·기관 수급 창을 연다. 이미 열려 있으면 그 종목으로 바꾼다."""
        if self.selection.market in ("US","US_SURGE"):
            self.status_label.configure(text="외국인·기관 수급은 국내 종목만 지원합니다",foreground="#b54708")
            return
        if self.settings_pending or self.settings_dialog is not None or not self.snapshot:
            return
        selected = self.tree.selection()
        quote = next((q for q in self.snapshot.quotes if selected and q.code == selected[0]),None)
        if quote is None:
            self.status_label.configure(text="표에서 종목을 먼저 선택한 뒤 수급 버튼을 누르세요",foreground="#b54708")
            return
        if self.flow_window is not None:
            self.flow_window.select_stock(quote)
            self.flow_window.lift()
            return
        self.flow_window = InvestorWindow(self,quote=quote)

    def open_settings(self,lock_after=False):
        if self.settings_dialog is not None:
            self.settings_dialog.lift()
            return
        self.stop()
        if self.aux_window is not None:
            self.aux_window.close()
        if self.flow_window is not None:
            self.flow_window.close()
        self.settings_pending = True
        self.lock_pending = self.lock_pending or lock_after
        self.status_label.configure(text="KIS 설정 준비: 진행 중인 요청을 마치는 중…")

    def show_settings(self):
        self.settings_pending = False
        if self.lock_pending:
            self.client.lock()
            self.lock_pending = False
            self.status_label.configure(text="키 잠금 완료 · 저장한 키는 비밀번호로 해제하고, 저장하지 않은 키는 다시 입력하세요")
        self.settings_dialog = CredentialDialog(self.root,self.client,self.settings_done)

    def settings_done(self,changed):
        self.settings_dialog = None
        if changed:
            self.view_cache.clear()
            self.snapshot = None
            self.references.clear()
            self.tree.delete(*self.tree.get_children())
            self.last_history_request = None
            self.history_not_before = 0
            self.failures = 0
            self.historical_retry_needed = False
            if self.client.configured:
                self.refresh()
            else:
                self.status_label.configure(text="키가 잠겨 있거나 삭제되었습니다. KIS 설정에서 잠금 해제 또는 새 키 입력이 필요합니다")

    def cancel_timer(self,name):
        if name == "auto_timer":
            self.historical_retry_at = 0.0
        timer = getattr(self,name)
        if timer is not None:
            self.root.after_cancel(timer)
            setattr(self,name,None)

    def choose_day(self,days_ago):
        self.date_var.set((core.now_kst().date()-dt.timedelta(days=days_ago)).isoformat())
        self.apply_selection()

    def tab_changed(self,_event):
        market = self.tab_markets[self.tabs.index(self.tabs.select())]
        if market != self.selection.market or (self.stopped and self.snapshot is None):
            self.apply_selection()

    def apply_selection(self):
        """날짜·시장이 바뀌면 이전 표를 지우고, 취소된 요청의 늦은 결과도 버린다."""
        try:
            market = self.tab_markets[self.tabs.index(self.tabs.select())]
            mode = "volume" if self.mode_var.get() == "거래량" else "value"
            if market in ("US","US_SURGE"):
                # 미국 순위 API는 날짜를 받지 않는다. 입력된 날짜와 무관하게 항상 최신 순위다.
                selection = core.Selection(None,mode,market)
            else:
                date = core.valid_date(self.date_var.get().strip())
                today = core.now_kst().date().isoformat()
                if date > today:
                    raise core.DataError("미래의 날짜는 선택할 수 없습니다")
                selection = core.Selection(None if date == today else date,mode,market)
        except core.DataError as exc:
            self.stop()
            self.historical_retry_needed = False
            self.snapshot = None
            self.references.clear()
            self.tree.delete(*self.tree.get_children())
            self.detail_label.configure(text="")
            self.reference_label.configure(text="")
            self.status_label.configure(text=str(exc),foreground="#b42318")
            return
        if selection != self.selection:
            if self.snapshot is not None:
                self.view_cache[self.selection] = (self.snapshot,dict(self.references))
            self.cancel_timer("auto_timer")
            self.live.stop()
            self.history.stop()
            self.selection = selection
            self.apply_columns()
            self.snapshot = None
            self.references.clear()
            self.last_history_request = None
            self.history_not_before = 0
            self.failures = 0
            self.historical_retry_needed = False
            self.tree.delete(*self.tree.get_children())
            self.detail_label.configure(text="")
            self.reference_label.configure(text="")
            cached = self.view_cache.get(selection)
            if cached:
                # 이전에 받은 화면을 즉시 보여 준다. 장중 오늘 화면은 뒤에서 새로 갱신한다.
                self.snapshot = cached[0]
                self.references.update(cached[1])
                today = core.now_kst().date().isoformat()
                if market not in ("US","US_SURGE"):
                    self.tree.heading("price",text="종가(원)" if selection.date is not None or self.snapshot.data_date < today else "현재가(원)")
                self.render()
                self.detail_label.configure(text=f"저장된 화면 · 수신 {self.snapshot.received_at:%H:%M:%S} KST")
                if self.snapshot.complete and market not in ("US","US_SURGE") and (selection.date is not None or core.now_kst().weekday() >= 5):
                    # 과거·주말 자료는 더 바뀌지 않으므로 다시 조회하지 않는다.
                    self.stopped = False
                    scope = core.MARKETS.get(selection.market,"전체")
                    self.status_label.configure(text=f"{scope} {core.MODES[selection.mode]} TOP100 · 거래일 {self.snapshot.data_date} · 비교일 {self.snapshot.previous_date} · 저장된 화면 (재조회 없음)",foreground="#237347")
                    self.stop_button.configure(state=tk.NORMAL)
                    self.schedule_live()
                    self.ensure_history()
                    return
        self.refresh()

    def refresh(self):
        """선택한 날짜만 조회한다. 과거 전체 종목 조회는 날짜·탭 선택이나 조회 버튼으로 시작한다."""
        if self.closed or self.settings_pending or self.settings_dialog is not None:
            return
        if not self.client.configured:
            self.open_settings()
            return
        if self.stopped:
            self.history_not_before = 0
            self.last_history_request = None
        self.stopped = False
        self.historical_retry_needed = False
        self.cancel_timer("auto_timer")
        self.live.submit(self.selection)
        self.stop_button.configure(state=tk.NORMAL)
        label = "미국 급등주" if self.selection.market == "US_SURGE" else "미국" if self.selection.market == "US" else "오늘" if self.selection.date is None else self.selection.date
        self.status_label.configure(text=f"{label} 조회 중…" if self.selection.market == "US_SURGE" else f"{label} {core.MODES[self.selection.mode]} TOP100 조회 중…",foreground="#333333")
        self.detail_label.configure(text=self.live_service.historical.LIMITATION if self.selection.date else "")
        self.next_label.configure(text="과거 최초 조회는 전체 종목 확인으로 시간이 걸릴 수 있습니다" if self.selection.date else "")

    def schedule_live(self,server_delay=0):
        self.cancel_timer("auto_timer")
        if self.closed or self.stopped:
            return
        if self.selection.date is not None:
            if self.historical_retry_needed:
                delay = core.retry_delay(5,self.failures,server_delay)
                self.historical_retry_at = max(time.monotonic()+delay,self.client.not_before)
                selection = self.selection
                self.auto_timer = self.root.after(math.ceil((self.historical_retry_at-time.monotonic())*1000),
                                                 lambda:self.retry_historical(selection))
                self.show_retry_countdown()
            else:
                self.next_label.configure(text="과거 날짜: 자동 갱신 없음")
            return
        if self.selection.market in ("US","US_SURGE"):
            closed = us_weekend()
            if self.selection.market == "US_SURGE":
                pending = bool(self.snapshot and any(q.trade_value is None or q.volume is None or q.open_price is None or q.previous_trade_value is None or q.previous_volume is None for q in self.snapshot.quotes))
                if pending:
                    delay = max(server_delay,US_ACTIVE_REFRESH_SECONDS)
                    self.next_label.configure(text=f"급등주 상세값 보충 조회 약 {delay:g}초 후")
                    self.auto_timer = self.root.after(max(1,int(delay*1000)),self.auto_tick)
                    return
            if closed:
                self.next_label.configure(text="미국 주말: 자동 접속 중지" if self.selection.market == "US" else "미국 휴장 · 급등주 보충 조회 완료")
                return
            delay = core.retry_delay(US_ACTIVE_REFRESH_SECONDS if active_window() else 60,
                                     min(self.failures,3),server_delay)
            self.next_label.configure(text=f"다음 조회 약 {delay:g}초 후")
            self.auto_timer = self.root.after(max(1,int(delay*1000)),self.auto_tick)
            return
        weekend = core.now_kst().weekday() >= 5
        now = core.now_kst()
        trading = 9*60 <= now.hour*60+now.minute <= 15*60+40
        delay = max(server_delay,60) if weekend else core.retry_delay(0.5 if trading else 5,min(self.failures,3) if trading else self.failures,server_delay)
        self.next_label.configure(text="주말: 자동 접속 중지" if weekend else f"다음 조회 약 {delay:g}초 후")
        self.auto_timer = self.root.after(max(1,int(delay*1000)),self.auto_tick)

    def auto_tick(self):
        """주말에는 재접속하지 않고 월요일이 되면 오늘 조회를 재개한다."""
        self.auto_timer = None
        if self.selection.market in ("US","US_SURGE"):
            if us_weekend():
                if self.selection.market == "US_SURGE" and self.snapshot and any(q.trade_value is None or q.volume is None or q.open_price is None or q.previous_trade_value is None or q.previous_volume is None for q in self.snapshot.quotes):
                    self.refresh()
                else:
                    self.schedule_live()
            else:
                self.refresh()
        elif core.now_kst().weekday() >= 5:
            self.schedule_live()
        else:
            self.date_var.set(core.now_kst().date().isoformat())
            self.refresh()

    def ensure_history(self):
        """표에 표시할 종목의 전거래일 값을 준비한다. 순위 순서만 바뀌면 다시 받지 않는다."""
        if self.stopped or self.snapshot is None or self.selection.market in ("US","US_SURGE") or time.monotonic() < self.history_not_before:
            return
        wanted = core.ReferenceSelection(self.snapshot.previous_date,tuple(sorted(q.code for q in self.snapshot.quotes)),
                                        core.now_kst().date().isoformat())
        if wanted == self.last_history_request:
            return
        if self.history.busy and not self.history.cancel.is_set() and self.history.active[1].date == wanted.date and self.history.pending is None:
            return
        self.history.submit(wanted)
        self.last_history_request = wanted

    def poll(self):
        """작업 결과만 메인 스레드에서 표시한다. 다른 탭·날짜의 늦은 결과는 적용하지 않는다."""
        if self.closed:
            return
        dirty = False
        while True:
            try:
                key, value = self.index_events.get_nowait()
            except queue.Empty:
                break
            if key in self.index_vars and key != "kst":
                self.index_vars[key].set(value)
        while True:
            try:
                progress = self.live_service.historical.progress.get_nowait()
            except queue.Empty:
                break
            if not self.stopped and self.live.busy and progress.selection == self.selection:
                self.status_label.configure(text=progress.message)
        event = self.live.poll()
        if event is not None and event.selection == self.selection:
            delay = 0
            try:
                if event.error:
                    raise event.error
                snapshot = event.result
                us = self.selection.market in ("US","US_SURGE")
                if not us and (not snapshot.previous_date or snapshot.previous_date >= snapshot.data_date):
                    raise core.DataError("직전 거래일이 확인되지 않았습니다")
                if not self.snapshot or self.snapshot.previous_date != snapshot.previous_date:
                    self.references.clear()
                    self.last_history_request = None
                    self.history_not_before = 0
                self.snapshot = snapshot
                provisional = any(w.startswith("임시 순위") for w in snapshot.warnings)
                self.failures = 0 if snapshot.complete or provisional else min(6,self.failures+1)
                delay = snapshot.retry_after
                self.historical_retry_needed = not snapshot.complete and not provisional
                scope = "미국 급등주" if self.selection.market == "US_SURGE" else "미국" if us else core.MARKETS.get(self.selection.market,"전체")
                quality = "수집 완료" if snapshot.complete else ("수집 중 · 임시 순위" if provisional else "일부 조회 실패 · 순위 불완전")
                fallback = " · 오늘 거래 기록 없음: 최근 거래일 표시" if not us and self.selection.date is None and snapshot.data_date != core.now_kst().date().isoformat() else ""
                basis = (f"수신 {snapshot.received_at:%H:%M:%S} KST · 미국 동부 {eastern_now(snapshot.received_at):%m-%d %H:%M}" if us
                         else f"거래일 {snapshot.data_date} · 비교일 {snapshot.previous_date}")
                self.status_label.configure(text=f"{scope} {core.MODES[self.selection.mode]} TOP100 · {basis} · {quality}{fallback}",
                                            foreground="#237347" if snapshot.complete else ("#b54708" if provisional else "#b42318"))
                self.detail_label.configure(text=f"조회 완료 {snapshot.received_at:%Y-%m-%d %H:%M:%S} KST" +
                                            (" · " + " / ".join(snapshot.warnings) if snapshot.warnings else ""))
                if not us:
                    historical = self.selection.date is not None or snapshot.data_date < core.now_kst().date().isoformat()
                    self.tree.heading("price",text="종가(원)" if historical else "현재가(원)")
                dirty = True
                self.ensure_history()
            except Exception as exc:
                self.failures = min(6,self.failures+1)
                delay = getattr(exc,"retry_after",0)
                self.historical_retry_needed = delay > 0
                suffix = "이전 수신 표 유지 · 갱신 실패" if self.snapshot else "표시할 자료 없음"
                self.status_label.configure(text=f"조회 실패: {exc} · {suffix}",foreground="#b42318")
            self.schedule_live(delay)
        while True:
            try:
                progress = self.reference_service.progress.get_nowait()
            except queue.Empty:
                break
            if not self.stopped and self.snapshot and progress.date == self.snapshot.previous_date:
                # 같은 종목의 일봉 진행 이벤트가 먼저 와도 이미 확인한 업종명은 유지한다.
                old = self.references.get(progress.code)
                if progress.sector is None and old and old.sector:
                    progress = core.ReferenceProgress(progress.date,progress.code,progress.record,progress.status,old.sector)
                self.references[progress.code] = progress
                dirty = True
        event = self.history.poll()
        if event is not None:
            delay = max(60,getattr(event.error,"retry_after",0)) if event.error else event.result.retry_after
            if event.error:
                self.reference_label.configure(text=f"전거래일 조회 실패: {event.error}")
            if delay:
                self.history_not_before = time.monotonic()+delay
                self.last_history_request = None
            self.ensure_history()
        if not self.history.busy and self.last_history_request is None:
            self.ensure_history()
        if dirty:
            self.render()
        for worker in self.retired_aux:
            worker.poll()
        self.retired_aux = [worker for worker in self.retired_aux if worker.busy]
        if self.settings_pending and not self.live.busy and not self.history.busy and not self.retired_aux:
            self.show_settings()
        self.stop_button.configure(state=tk.NORMAL if not self.stopped and (self.live.busy or self.history.busy or self.auto_timer) else tk.DISABLED)
        self.show_retry_countdown()
        self.poll_timer = self.root.after(100,self.poll)

    def update_kst_clock(self):
        if self.closed:
            return
        self.index_vars["kst"].set(core.now_kst().strftime("%Y-%m-%d %H:%M:%S KST"))
        self.index_timer = self.root.after(1000,self.update_kst_clock)

    @staticmethod
    def _format_index(row, price_keys, rate_keys, suffix=""):
        price = core.first_number(row,price_keys,nonnegative=True)
        if price is None:
            return None
        rate = core.first_number(row,rate_keys)
        text = f"{price:,.2f}{suffix}"
        if rate is not None:
            text += f"  ({rate:+.2f}%)"
        return text

    def market_index_worker(self):
        """KIS 지수 스냅샷은 작업 스레드에서 조회하고 Tk에는 이벤트로 전달한다."""
        while not self.index_cancel.is_set():
            if not self.client.configured:
                self.index_cancel.wait(2)
                continue
            for key, code in (("kospi","0001"),("kosdaq","1001")):
                if self.index_cancel.is_set():
                    return
                try:
                    data = self.client.get("/uapi/domestic-stock/v1/quotations/inquire-index-price",
                        {"FID_COND_MRKT_DIV_CODE":"U","FID_INPUT_ISCD":code},self.index_cancel)
                    row = data.get("output")
                    if not isinstance(row,dict):
                        raise core.DataError("지수 응답 형식 오류")
                    value = self._format_index(row,("bstp_nmix_prpr",),("bstp_nmix_prdy_ctrt",)," pt")
                    if value is None:
                        raise core.DataError("지수 값 없음")
                    self.index_events.put((key,value))
                except core.Cancelled:
                    return
                except Exception as exc:
                    self.index_events.put((key,"조회 실패"))
                    core.LOG.warning("%s 지수 조회 실패: %s",key,exc)
            if self.index_cancel.is_set():
                return
            try:
                data = self.client.get("/uapi/overseas-price/v1/quotations/inquire-time-indexchartprice",
                    {"FID_COND_MRKT_DIV_CODE":"N","FID_INPUT_ISCD":"NDX",
                     "FID_HOUR_CLS_CODE":"0","FID_PW_DATA_INCU_YN":"Y"},self.index_cancel)
                rows = data.get("output1")
                if isinstance(rows,dict):
                    rows = [rows]
                if not isinstance(rows,list) or not rows:
                    rows = data.get("output2")
                if isinstance(rows,dict):
                    rows = [rows]
                row = next((r for r in rows if isinstance(r,dict) and self._format_index(
                    r,("last","ovrs_nmix_prpr","bstp_nmix_prpr","prpr","close"),
                    ("rate","ovrs_nmix_prdy_ctrt","bstp_nmix_prdy_ctrt","prdy_ctrt")) is not None),None) if isinstance(rows,list) else None
                value = self._format_index(row,("last","ovrs_nmix_prpr","bstp_nmix_prpr","prpr","close"),
                    ("rate","ovrs_nmix_prdy_ctrt","bstp_nmix_prdy_ctrt","prdy_ctrt")," USD") if row else None
                if value is None:
                    raise core.DataError("NASDAQ 100 지수 응답에 현재값이 없습니다")
                self.index_events.put(("ndx",value))
            except core.Cancelled:
                return
            except Exception as exc:
                self.index_events.put(("ndx","조회 실패"))
                core.LOG.warning("NASDAQ 100 지수 조회 실패: %s",exc)
            self.index_cancel.wait(5)

    def show_retry_countdown(self):
        if self.historical_retry_at and not self.stopped:
            remaining = max(0,math.ceil(max(self.historical_retry_at,self.client.not_before)-time.monotonic()))
            self.next_label.configure(text=f"{remaining}초 후 과거 조회 자동 재개 · 받은 자료 재사용")

    def retry_historical(self,selection):
        """Resume the same historical selection, including weekends, after the shared cooldown."""
        self.auto_timer = None
        if (self.closed or self.stopped or selection != self.selection
                or self.settings_pending or self.settings_dialog is not None):
            self.historical_retry_at = 0.0
            return
        remaining = max(self.historical_retry_at,self.client.not_before)-time.monotonic()
        if remaining > 0:
            self.auto_timer = self.root.after(max(1,math.ceil(remaining*1000)),lambda:self.retry_historical(selection))
            return
        self.refresh()

    def render(self):
        """대금과 거래량 각각의 차이·비율을 계산한다. 결측과 분모 0은 —로 표시한다."""
        if self.snapshot is None:
            return
        if self.selection.market in ("US","US_SURGE"):
            self.render_us()
            return
        rows = []
        complete = 0
        # 모든 날짜·시장 공통: 등락률이 +이면 빨강, −이면 파랑. 등락률이 없으면 색을 칠하지 않는다.
        for rank,q in enumerate(self.snapshot.quotes,1):
            progress = self.references.get(q.code)
            ref = progress.record if progress else None
            row = core.comparison_row(rank,q,ref)
            row["sector"] = progress.sector if progress and progress.sector else "조회 중"
            ready = ref is not None and ref.trade_value is not None and ref.volume is not None
            complete += ready
            row["state"] = "완료" if ready else "일부 비교 자료 없음" if ref else progress.status if progress else "전거래일 조회 중"
            rows.append(row)
        known = [r for r in rows if r[self.sort_key] is not None]
        missing = [r for r in rows if r[self.sort_key] is None]
        known.sort(key=lambda r:r[self.sort_key],reverse=self.sort_reverse)
        selected,view = self.tree.selection(),self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        for row in known+missing:
            values = [core.format_cell(key,row[key]) for key in self.columns]
            change = row["change_rate"]
            tag = "up" if change is not None and change>0 else "down" if change is not None and change<0 else ""
            self.tree.insert("",tk.END,iid=row["code"],values=values,tags=(tag,))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if view:
            self.tree.yview_moveto(view[0])
        self.reference_label.configure(text=f"직전 거래일 비교 자료: {complete}/{len(rows)}종목 · 해당일 자료가 없거나 전거래일 값이 0이면 증감률 —")

    US_COLUMNS = ("rank","name","sector","code","market","trade_value","previous_value","value_change","value_rate","open_price","price","change_rate","volume","volume_change")

    def apply_columns(self):
        """미국 탭은 국내와 같은 열을 표시하고 통화 단위를 달러로 바꾼다."""
        us = self.selection.market in ("US","US_SURGE")
        self.tree.configure(displaycolumns=self.US_COLUMNS if us else tuple(self.columns))
        self.tree.heading("trade_value",text=("추정 거래대금(달러)" if self.selection.market == "US_SURGE" else "거래대금(달러)") if us else self.columns["trade_value"][0])
        self.tree.heading("previous_value",text="전거래일 거래대금(달러)" if us else self.columns["previous_value"][0])
        self.tree.heading("open_price",text="시가(달러)" if us else self.columns["open_price"][0])
        self.tree.heading("price",text="현재가(달러)" if us else "현재가(원)")

    def us_cell(self,key,value):
        if value is None:
            if key == "sector":
                return "KIS 미제공"
            if self.selection.market == "US" and key in (
                    "trade_value", "previous_value", "value_change", "value_rate", "open_price", "volume_change"):
                if key == "open_price":
                    et = eastern_now()
                    if et.weekday() < 5 and et.hour * 60 + et.minute < 9 * 60 + 30:
                        return "정규장 전"
                return "조회 중"
            if self.selection.market == "US_SURGE" and key in ("trade_value","previous_value","value_change","value_rate","open_price","volume","volume_change"):
                return "조회 중"
            return "—"
        if key in ("trade_value","previous_value","value_change"):
            return format_usd(value)
        if key == "value_rate":
            return f"{value:+.2f}%"
        if key in ("open_price","price"):
            return f"{value:,.2f}"
        if key == "change_rate":
            return f"{value:+.2f}%"
        if key in ("volume","volume_change"):
            return f"{value:,}"
        return value

    def render_us(self):
        """미국 순위 표. 보충 중인 항목은 — 대신 조회 상태를 표시한다."""
        rows = []
        for rank,q in enumerate(self.snapshot.quotes,1):
            previous_value = q.previous_trade_value
            value_change = q.trade_value-previous_value if q.trade_value is not None and previous_value is not None else None
            volume_change = q.volume-q.previous_volume if q.volume is not None and q.previous_volume is not None else None
            rows.append({"rank":rank,"name":q.name,"sector":None,"code":q.code,"market":US_MARKETS.get(q.market,q.market),
                         "trade_value":q.trade_value,"previous_value":previous_value,"value_change":value_change,"value_rate":core.percentage(q.trade_value,previous_value),
                         "open_price":q.open_price,"price":q.price,"change_rate":q.change_rate,
                         "volume":q.volume,"volume_change":volume_change,"iid":f"{q.market}:{q.code}"})
        key = self.sort_key if self.sort_key in self.US_COLUMNS else "rank"
        reverse = self.sort_reverse if key == self.sort_key else False
        known = sorted((r for r in rows if r[key] is not None),key=lambda r:r[key],reverse=reverse)
        missing = [r for r in rows if r[key] is None]
        selected,view = self.tree.selection(),self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        for row in known+missing:
            rate = row["change_rate"]
            tag = "up" if rate is not None and rate>0 else "down" if rate is not None and rate<0 else ""
            self.tree.insert("",tk.END,iid=row["iid"],tags=(tag,),
                             values=[self.us_cell(k,row.get(k)) for k in self.US_COLUMNS])
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if view:
            self.tree.yview_moveto(view[0])
        note = ("상승률 기준 자동 갱신 · 현재/전일 거래대금 API 값이 없으면 가격×거래량 추정 · 전일 비교와 시가는 보충 조회 중 표시"
                if self.selection.market == "US_SURGE" else
                "시가·전거래일 비교는 종목별 일봉으로 순차 보충 · 거래대금 순위에 없는 값은 가격×거래량 추정 · 업종은 KIS 미제공")
        self.reference_label.configure(text=f"미국 {len(rows)}종목 · {note}")

    def sort_column(self,key):
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = key not in ("rank","name","sector","code","market","state")
        self.render()

    def stop(self):
        """긴 과거 조회 또는 자동 갱신을 중지한다. 조회 버튼을 누르면 다시 시작한다."""
        self.historical_retry_needed = False
        self.stopped = True
        self.cancel_timer("auto_timer")
        self.live.stop()
        self.history.stop()
        self.last_history_request = None
        self.status_label.configure(text="조회 중지 · 받은 과거 자료는 저장되어 재조회 시 재사용합니다",foreground="#333333")
        self.next_label.configure(text="")

    def close(self):
        """취소 신호를 보내고 창을 닫는다. 오늘 시세를 파일로 저장하지 않는다."""
        if self.closed:
            return
        self.closed = True
        if self.aux_window is not None:
            self.aux_window.close()
        if self.flow_window is not None:
            self.flow_window.close()
        for worker in self.retired_aux:
            worker.close()
        if self.settings_dialog is not None:
            self.settings_dialog.cancel.set()
        self.live.close()
        self.history.close()
        self.cancel_timer("auto_timer")
        self.cancel_timer("poll_timer")
        self.cancel_timer("index_timer")
        self.index_cancel.set()
        self.root.destroy()


class CredentialDialog(tk.Toplevel):
    """All Tk work stays on the main thread; authentication runs in a worker."""
    def __init__(self,parent,client,on_done):
        super().__init__(parent)
        self.client, self.on_done = client,on_done
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.busy = False
        self.timer = None
        self.title("KIS 키 보호 / 잠금 해제")
        self.resizable(False,False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW",self.dismiss)
        frame = ttk.Frame(self,padding=20)
        frame.pack(fill=tk.BOTH,expand=True)
        ttk.Label(frame,text="한국투자증권 API 키 보호",font=("맑은 고딕",11,"bold")).grid(row=0,column=0,columnspan=2,pady=(0,10))
        ttk.Label(frame,wraplength=620,text="기본은 이번 실행에서만 사용합니다. 저장하려면 별도 잠금 비밀번호가 필요합니다.\n"
            "이 앱은 시세만 조회하지만 키 자체의 주문 권한은 제한하지 못합니다.\n"
            "실행 중 악성코드로부터 완전한 보호는 불가능합니다. 유출 의심 시 한투에서 키를 재발급하세요.").grid(row=1,column=0,columnspan=2,sticky="w",pady=(0,12))
        ttk.Label(frame,text="App Key").grid(row=2,column=0,sticky="w")
        ttk.Label(frame,text="App Secret").grid(row=3,column=0,sticky="w")
        self.key = ttk.Entry(frame,width=52,show="*")
        self.secret = ttk.Entry(frame,width=52,show="*")
        self.key.grid(row=2,column=1,padx=10,pady=5)
        self.secret.grid(row=3,column=1,padx=10,pady=5)
        self.remember = tk.BooleanVar(value=False)
        self.remember_check = ttk.Checkbutton(frame,text="이 PC에 비밀번호로 잠가 저장",variable=self.remember,command=self.storage_changed)
        self.remember_check.grid(row=4,column=0,columnspan=2,sticky="w",pady=(8,0))
        ttk.Label(frame,text="새 잠금 비밀번호").grid(row=5,column=0,sticky="w")
        self.password = ttk.Entry(frame,width=52,show="*",state=tk.DISABLED)
        self.password.grid(row=5,column=1,padx=10,pady=5)
        ttk.Label(frame,text="비밀번호 확인").grid(row=6,column=0,sticky="w")
        self.confirm = ttk.Entry(frame,width=52,show="*",state=tk.DISABLED)
        self.confirm.grid(row=6,column=1,padx=10,pady=5)
        ttk.Label(frame,wraplength=620,text="12~128자의 고유한 비밀번호를 사용하세요. 분실 시 복구할 수 없어 키를 다시 입력해야 합니다.\n"
            "새 키 적용·기존 키 전환에 성공하면 기존 저장 파일을 대체합니다. 저장을 끄면 기존 파일을 삭제합니다.").grid(row=7,column=0,columnspan=2,sticky="w",pady=5)
        try:
            self.kind = client.store.saved_kind()
        except core.DataError:
            self.kind = "unreadable"
        self.unlock_password = ttk.Entry(frame,width=52,show="*")
        self.unlock_button = ttk.Button(frame,text="저장된 키 잠금 해제",command=self.unlock)
        if self.kind == "password":
            ttk.Separator(frame).grid(row=8,column=0,columnspan=2,sticky="ew",pady=8)
            ttk.Label(frame,text="기존 잠금 비밀번호").grid(row=9,column=0,sticky="w")
            self.unlock_password.grid(row=9,column=1,padx=10,pady=5)
            self.unlock_button.grid(row=10,column=1,sticky="e",padx=10)
        self.migrate_button = ttk.Button(frame,text="기존 키를 위에서 선택한 방식으로 전환",command=self.migrate)
        if self.kind == "legacy":
            self.migrate_button.grid(row=10,column=0,columnspan=2,sticky="w",pady=8)
        descriptions = {
            "password": "저장된 키는 잠겨 있습니다. 기존 잠금 비밀번호로 해제하거나 새 키를 입력하세요.",
            "legacy": "이전 버전의 저장 키가 있습니다. 자동으로 읽지 않습니다. 저장 여부를 선택한 뒤 전환하세요.",
            "none": "실전투자용 키를 입력하세요. 모의투자 키는 지원하지 않습니다.",
            "unreadable": "키 파일에 접근할 수 없습니다. 파일 권한을 확인하세요."}
        current = "현재 키 사용 중 · " + ("비밀번호로 저장됨" if client.store.persistent else "이번 실행만 사용") if client.configured else descriptions[self.kind]
        self.label = ttk.Label(frame,wraplength=620,text=current)
        self.label.grid(row=11,column=0,columnspan=2,pady=10,sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=12,column=0,columnspan=2,sticky="e")
        self.save = ttk.Button(buttons,text="연결 확인 및 사용",command=self.submit)
        self.save.pack(side=tk.LEFT,padx=4)
        self.remove = ttk.Button(buttons,text="PC 키 삭제",command=self.clear)
        self.remove.pack(side=tk.LEFT,padx=4)
        self.revoke_button = ttk.Button(buttons,text="토큰 폐기 + PC 키 삭제",command=self.revoke)
        self.revoke_button.pack(side=tk.LEFT,padx=4)
        self.exit_button = ttk.Button(buttons,text="닫기",command=self.dismiss)
        self.exit_button.pack(side=tk.LEFT,padx=4)
        self.widgets = (self.save,self.remove,self.revoke_button,self.key,self.secret,self.remember_check,
                        self.password,self.confirm,self.unlock_password,self.unlock_button,self.migrate_button,self.exit_button)
        self.storage_changed()
        (self.unlock_password if self.kind == "password" and not client.configured else self.key).focus_set()

    def storage_changed(self):
        enabled = self.remember.get() and not self.busy
        for entry in (self.password,self.confirm):
            entry.configure(state=tk.NORMAL)
            if not enabled:
                entry.delete(0,tk.END)
                entry.configure(state=tk.DISABLED)
        self.save.configure(text="연결 확인 및 보호 저장" if self.remember.get() else "연결 확인 및 사용")
        self.revoke_button.configure(state=tk.NORMAL if self.client.configured and not self.busy else tk.DISABLED)

    def selected_password(self):
        if not self.remember.get():
            return None
        password = self.password.get()
        validate_password(password)
        if password != self.confirm.get():
            raise core.DataError("잠금 비밀번호와 확인 값이 다릅니다")
        return password

    def erase_inputs(self):
        for entry in (self.key,self.secret,self.password,self.confirm,self.unlock_password):
            entry.configure(state=tk.NORMAL)
            entry.delete(0,tk.END)

    def run_task(self, task, message, action="configure"):
        self.erase_inputs()
        self.busy = True
        self.action = action
        for widget in self.widgets:
            widget.configure(state=tk.DISABLED)
        self.label.configure(text=message,foreground="#333333")
        def work():
            try:
                self.events.put((task(),None))
            except Exception as exc:
                self.events.put((None,str(exc) if isinstance(exc,core.DataError) else "설정 처리에 실패했습니다. 다시 시도하세요"))
        threading.Thread(target=work,daemon=True,name="kis-setup").start()
        self.timer = self.after(100,self.poll_result)

    def submit(self):
        if self.busy:
            return
        key,secret = self.key.get(),self.secret.get()
        try:
            password = self.selected_password()
        except core.DataError as exc:
            self.label.configure(text=str(exc),foreground="#b42318")
            return
        self.run_task(lambda:self.client.configure(key,secret,self.cancel,password=password),"KIS 연결 확인 및 키 보호 설정 중…")

    def unlock(self):
        if not self.busy:
            password = self.unlock_password.get()
            self.run_task(lambda:self.client.unlock(password),"저장된 키 잠금 해제 중…")

    def migrate(self):
        if self.busy:
            return
        try:
            password = self.selected_password()
        except core.DataError as exc:
            self.label.configure(text=str(exc),foreground="#b42318")
            return
        self.run_task(lambda:self.client.migrate_legacy(password),"기존 키 보호 방식 전환 중…")

    def poll_result(self):
        self.timer = None
        try:
            result,error = self.events.get_nowait()
        except queue.Empty:
            self.timer = self.after(100,self.poll_result)
            return
        self.busy = False
        for widget in self.widgets:
            widget.configure(state=tk.NORMAL)
        self.storage_changed()
        if error:
            self.label.configure(text=error,foreground="#b42318")
        else:
            if self.action == "revoke":
                message = ("현재 토큰의 서버 폐기와 PC 키 삭제를 완료했습니다." if result else
                    "PC 키는 삭제했지만 서버 토큰 폐기를 확인하지 못했습니다.")
                messagebox.showinfo("키 삭제 결과",message + "\nApp Key·Secret 자체는 폐기되지 않습니다.\n유출 의심 시 한투에서 키를 재발급하세요.",parent=self)
            elif self.action == "delete":
                messagebox.showinfo("PC 키 삭제", "PC의 키만 삭제했습니다. 서버의 키와 토큰은 유효할 수 있습니다.\n유출 의심 시 한투에서 키를 재발급하세요.",parent=self)
            self.finish(True)

    def clear(self):
        if not self.busy and messagebox.askyesno("PC 키 삭제", "PC의 저장 키와 현재 사용 중인 키를 삭제합니다.\n한투 서버의 키·토큰은 폐기되지 않습니다. 계속할까요?",parent=self):
            self.run_task(self.client.clear,"PC 키 삭제 중…",action="delete")

    def revoke(self):
        if not self.busy and self.client.configured and messagebox.askyesno("토큰 폐기 및 PC 키 삭제",
                "현재 토큰의 서버 폐기를 요청한 뒤 PC 키를 삭제합니다.\n같은 토큰을 쓰는 다른 프로그램도 영향을 받을 수 있습니다.\n통신에 실패해도 PC 키는 삭제합니다. 계속할까요?",parent=self):
            self.run_task(lambda:self.client.revoke_and_clear(self.cancel),"토큰 폐기 요청 및 PC 키 삭제 중…",action="revoke")

    def dismiss(self):
        if self.busy:
            self.label.configure(text="연결 확인이 끝난 뒤 닫을 수 있습니다.")
            return
        self.finish(False)

    def finish(self,changed):
        self.cancel.set()
        self.erase_inputs()
        if self.timer:
            self.after_cancel(self.timer)
        self.grab_release()
        self.destroy()
        self.on_done(changed)


def main():
    root = tk.Tk()
    root.withdraw()
    def callback_error(exc_type, _value, _traceback):
        core.LOG.warning("화면 처리 실패 (%s)",exc_type.__name__)
        messagebox.showerror("화면 처리 오류","처리 중 오류가 발생했습니다. 앱을 다시 시작해 주세요.",parent=root)
    root.report_callback_exception = callback_error
    lock = InstanceLock()
    try:
        lock.acquire()
        purge_legacy_data()
        directory = core.user_data_dir()
        directory.mkdir(parents=True,exist_ok=True)
        handler = RotatingFileHandler(directory/"kis_mars.log",maxBytes=1_000_000,backupCount=2,encoding="utf-8")
        core.LOG.addHandler(handler)
        core.LOG.setLevel(logging.WARNING)
        store = SecureStore()
        try:
            client = KISClient(store)
        except core.DataError as exc:
            messagebox.showwarning("API 설정 확인",str(exc),parent=root)
            client = KISClient(store,state={})
        StockApp(root,client=client)
        root.deiconify()
        root.after(500,open_startup_blog)
        root.mainloop()
    except (core.DataError,OSError) as exc:
        messagebox.showerror("마스 시작 실패",str(exc),parent=root)
        root.destroy()
    finally:
        lock.close()


def open_startup_blog():
    """프로그램 시작 시 안내 블로그를 기본 브라우저에서 연다."""
    try:
        webbrowser.open(STARTUP_BLOG_URL,new=2)
    except Exception as exc:
        core.LOG.warning("시작 블로그를 열지 못했습니다: %s",exc)


if __name__ == "__main__":
    main()














