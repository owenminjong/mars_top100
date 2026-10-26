# -*- coding: utf-8 -*-
"""실시간 시세 수신, 과거 일봉 캐시, 비동기 작업 제어. Tk에 의존하지 않는 핵심 로직."""
from __future__ import annotations
import datetime as dt
from contextlib import contextmanager, closing
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import logging
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

KST = dt.timezone(dt.timedelta(hours=9), "KST")
MODES = {"value": "거래대금", "volume": "거래량"}
MARKETS = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
LOG = logging.getLogger("mars")


def now_kst():
    return dt.datetime.now(KST)


class DataError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


def number(value):
    """결측·비정상 숫자와 실제 0을 구분. 과도하게 긴 숫자도 거부."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text or len(text) > 60:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    # SQLite 정수 범위 안에서만 받는다. 손상된 서버 숫자로 저장 단계가 넘치지 않게 한다.
    return result if result.is_finite() and abs(result) <= Decimal("9e18") else None


def first_number(row, keys, nonnegative=False, integral=False):
    for key in keys:
        result = number(row.get(key))
        if result is None or (nonnegative and result < 0):
            continue
        if integral and result != result.to_integral_value():
            continue
        return int(result) if integral else result
    return None


def first_text(row, keys, limit=100):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())
            if value and len(value) <= limit:
                return value
    return None


def stock_code(row):
    for key in ("code", "inter_shrn_iscd", "stck_shrn_iscd"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        code = str(value).strip().upper()
        code = re.sub(r"\.(KS|KQ)$", "", code)
        if code.isascii() and code.isdigit():
            code = code.zfill(6)
        # 일부 국내 상품 코드는 영문자를 포함한다.
        if re.fullmatch(r"[0-9A-Z]{6}", code):
            return code
    raise DataError("유효한 종목코드가 없습니다")


def parse_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise DataError("시세 기준시각이 없습니다")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataError("시세 기준시각 형식이 잘못되었습니다") from exc
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DataError("조회 날짜 형식이 잘못되었습니다")
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise DataError("유효하지 않은 날짜입니다") from exc


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    market: str
    price: int
    change_rate: Decimal | None
    trade_value: int | None
    volume: int | None
    as_of: dt.datetime | None
    sector: str = "미분류"
    open_price: int | None = None
    previous_trade_value: int | None = None
    previous_volume: int | None = None

    def metric(self, mode):
        return self.trade_value if mode == "value" else self.volume


@dataclass(frozen=True)
class Snapshot:
    mode: str
    data_date: str
    received_at: dt.datetime
    quotes: tuple[Quote, ...]
    statuses: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()
    complete: bool = True
    retry_after: int = 0
    previous_date: str = ""


def selected_markets(market):
    """전체는 두 시장을 합쳐 100개, 개별 시장은 그 시장 안에서 100개를 선정한다."""
    if market == "ALL":
        return tuple(MARKETS)
    if market in MARKETS:
        return (market,)
    raise DataError("지원하지 않는 시장입니다")


@dataclass(frozen=True)
class Selection:
    date: str | None
    mode: str
    market: str = "ALL"


@dataclass(frozen=True)
class Completion:
    request_id: int
    selection: Selection
    result: object | None
    error: Exception | None


class FetchController:
    """Tk와 무관한 단일 작업 제어기. 대기는 최신 선택 하나만 유지한다."""
    def __init__(self, service):
        self.service = service
        self.events = queue.Queue()
        self.generation = 0
        self.active = None
        self.pending = None
        self.cancel = None
        self.closed = False

    @property
    def busy(self):
        return self.active is not None

    def submit(self, selection):
        """겹치는 동일 요청은 생략하고 선택 변경은 가장 마지막 요청 하나로 합친다."""
        if self.closed:
            return False
        if self.active and not self.cancel.is_set() and selection == self.active[1] and self.pending is None:
            return False
        if self.pending and selection == self.pending[1]:
            return False
        self.generation += 1
        request = (self.generation, selection)
        if self.active:
            self.cancel.set()
            self.pending = request
        else:
            self._start(request)
        return True

    def _start(self, request):
        self.active = request
        self.cancel = threading.Event()
        threading.Thread(target=self._work, args=(*request, self.cancel),
                         name="mars-fetch", daemon=True).start()

    def _work(self, request_id, selection, cancel):
        result = error = None
        try:
            result = self.service.query(selection, cancel)
            check_cancel(cancel)
        except Cancelled as exc:
            error = exc
        except Exception as exc:
            error = exc if isinstance(exc, DataError) else DataError("조회 중 오류가 발생했습니다. 다시 시도하세요")
            LOG.warning("조회 실패 (%s)", type(exc).__name__)
        # worker에서는 Tk 위젯 및 root.after()에 접근하지 않는다.
        self.events.put(Completion(request_id, selection, result, error))

    def poll(self):
        """취소되거나 이전 선택에서 늦게 도착한 결과를 버리고 최신 요청만 전달한다."""
        if self.closed:
            return None
        try:
            event = self.events.get_nowait()
        except queue.Empty:
            return None
        if not self.active or event.request_id != self.active[0]:
            return None
        self.active = None
        if self.pending:
            request, self.pending = self.pending, None
            self._start(request)
            return None
        return event if event.request_id == self.generation else None

    def close(self):
        self.closed = True
        self.generation += 1
        self.pending = None
        if self.cancel:
            self.cancel.set()

    def stop(self):
        """화면 선택이 바뀌면 이전 작업과 대기를 취소하되 제어기는 재사용한다."""
        self.generation += 1
        self.pending = None
        if self.cancel:
            self.cancel.set()


def format_won(value):
    value = number(value)
    if value is None or value < 0:
        return "확인 불가"
    if value >= 1_000_000_000_000:
        return f"{value / Decimal(1_000_000_000_000):,.3f}조원"
    if value >= 100_000_000:
        return f"{value / Decimal(100_000_000):,.2f}억원"
    return f"{value:,.0f}원"


def retry_delay(interval, failures, server_delay=0):
    return max(server_delay, min(300, interval * (2 ** min(failures, 6))))


def user_data_dir():
    """EXE 설치 위치와 무관한 사용자별 저장소. 관리자 권한 없이 사용할 수 있다."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "MARS_TOP100"


@dataclass(frozen=True)
class DailyRecord:
    code: str
    date: str
    price: int
    volume: int | None
    trade_value: int | None = None
    source: str = "KIS KRX 일봉"
    as_of: str = ""
    open_price: int | None = None
    change_rate: Decimal | None = None


class DailyCache:
    """확인된 KRX 일별 시세만 저장한다. 오늘/미래 데이터는 저장 단계에서 제외한다."""
    def __init__(self, directory=None, clock=now_kst):
        self.directory = Path(directory) if directory is not None else user_data_dir()
        # KIS 전용 DB. 기존 네이버 저장소는 읽거나 가져오지 않는다.
        self.path = self.directory / "kis_krx_daily_v1.sqlite3"
        self.clock = clock
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=3)
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS daily (
                    code TEXT, day TEXT, price INTEGER, volume INTEGER, amount INTEGER,
                    open_price INTEGER, change_rate TEXT,
                    PRIMARY KEY(code,day))""")
                db.execute("""CREATE TABLE IF NOT EXISTS coverage_ranges (
                    code TEXT, start_day TEXT, end_day TEXT,
                    PRIMARY KEY(code,start_day,end_day))""")
                db.execute("""CREATE TABLE IF NOT EXISTS stock_sectors (
                    code TEXT PRIMARY KEY, sector TEXT, checked_day TEXT)""")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def is_past(self, date):
        return valid_date(date) < self.clock().date().isoformat()

    def save_records(self, records, coverage=None):
        """어제까지의 일별 가격·거래량·실제 거래대금을 저장한다."""
        self.save_batches(((records, coverage),))

    def save_batches(self, batches):
        """여러 종목을 한 트랜잭션으로 저장해 종목마다 디스크 동기화하지 않는다."""
        allowed, coverages = [], []
        for records, coverage in batches:
            allowed.extend(r for r in records if self.is_past(r.date))
            if coverage is not None:
                code, start, end = coverage
                if self.is_past(end) and valid_date(start) <= end:
                    coverages.append((code, start, end))
        if not allowed and not coverages:
            return
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?)", [
                (r.code, r.date, r.price, r.volume, r.trade_value, r.open_price,
                 str(r.change_rate) if r.change_rate is not None else None) for r in allowed])
            db.executemany("INSERT OR IGNORE INTO coverage_ranges VALUES (?,?,?)", coverages)

    def lookup(self, code, date):
        """정확히 그 날짜의 자료만 반환한다. 신규 상장 등 결측을 다른 날로 대체하지 않는다."""
        if not self.is_past(date):
            return None
        with self.connect() as db:
            row = db.execute("SELECT price,volume,amount,open_price,change_rate FROM daily WHERE code=? AND day=?", (code,date)).fetchone()
        return DailyRecord(code,date,*row[:3],source="KIS KRX 일봉",
                           open_price=row[3],change_rate=number(row[4])) if row else None

    def covered(self, code, date):
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM coverage_ranges WHERE code=? AND start_day<=? AND end_day>=? LIMIT 1",
                             (code,date,date)).fetchone()
        return row is not None

    def load_day(self, date):
        """과거 순위 재조회 때 수천 번 DB를 열지 않고 해당 날짜의 캐시를 한 번에 읽는다."""
        if not self.is_past(date):
            raise DataError("저장된 과거 자료만 읽을 수 있습니다")
        with self.connect() as db:
            rows = db.execute("SELECT code,price,volume,amount,open_price,change_rate FROM daily WHERE day=?", (date,)).fetchall()
            covered = {r[0] for r in db.execute(
                "SELECT DISTINCT code FROM coverage_ranges WHERE start_day<=? AND end_day>=?", (date,date))}
        records = {r[0]:DailyRecord(r[0],date,*r[1:4],open_price=r[4],change_rate=number(r[5])) for r in rows}
        return records, covered

    def lookup_sector(self, code):
        """업종은 시세와 별도인 현재 기업 정보다. 같은 날 확인한 이름만 재사용한다."""
        with self.connect() as db:
            row = db.execute("SELECT sector FROM stock_sectors WHERE code=? AND checked_day=?",
                             (code,self.clock().date().isoformat())).fetchone()
        return row[0] if row else None

    def save_sector(self, code, sector):
        self.save_sectors(((code, sector),))

    def load_sectors(self):
        with self.connect() as db:
            return dict(db.execute("SELECT code,sector FROM stock_sectors WHERE checked_day=?",
                                   (self.clock().date().isoformat(),)))

    def save_sectors(self, sectors):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO stock_sectors VALUES (?,?,?)",
                           [(code, sector, self.clock().date().isoformat()) for code, sector in sectors])


class WorkCancel:
    """A local pool cancellation signal also observes its parent selection signal."""
    def __init__(self, parent):
        self.parent = parent
        self.local = threading.Event()

    def is_set(self):
        return self.parent.is_set() or self.local.is_set()

    def set(self):
        self.local.set()

    def wait(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.local.wait(min(remaining, .05))
        return self.is_set()


def parallel_results(items, operation, cancel, workers=4):
    """At most four submitted jobs, shared KIS throttle, no unbounded task queue."""
    local = WorkCancel(cancel)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kis-read")
    pending = {}
    iterator = iter(items)
    def fill():
        while len(pending) < workers:
            check_cancel(local)
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending[executor.submit(operation, item, local)] = item
    try:
        fill()
        while pending:
            check_cancel(cancel)
            done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    value = future.result()
                except Cancelled:
                    raise
                except (DataError, OSError, sqlite3.Error) as exc:
                    yield item, None, exc
                else:
                    yield item, value, None
            fill()
    finally:
        local.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def daily_results(cache, provider, codes, target, cancel):
    """Read a date in one DB operation, fetch only missing symbols, commit in groups."""
    cached, covered = cache.load_day(target)
    missing, buffered = [], []
    for code in codes:
        check_cancel(cancel)
        record = cached.get(code)
        if ((record is not None and record.open_price is not None
             and record.trade_value is not None and record.volume is not None)
                or (record is None and code in covered)):
            yield code, record, None, True
        else:
            missing.append(code)
    try:
        operation = lambda code, signal: provider.fetch(code, target, signal)
        with closing(parallel_results(missing, operation, cancel)) as results:
            for code, batch, error in results:
                check_cancel(cancel)
                if error:
                    yield code, None, error, False
                    continue
                rows, coverage = batch
                buffered.append((rows, coverage))
                if len(buffered) >= 24:
                    cache.save_batches(buffered)
                    buffered.clear()
                yield code, next((r for r in rows if r.date == target), None), None, False
    finally:
        # Completed results remain reusable after cancellation or a server limit response.
        if buffered:
            cache.save_batches(buffered)


class LiveService:
    """오늘은 항상 새 응답, 과거 날짜는 일봉 조회로 분리한다. 오늘 값은 저장하지 않는다."""
    def __init__(self, provider, cache=None):
        self.provider = provider
        self.cache = cache or DailyCache()
        self.historical = HistoricalService(self.cache, provider.history, provider)

    def query(self, selection, cancel):
        if selection.date is not None:
            return self.historical.query(selection, cancel)
        return self.provider.fetch(selection.mode, cancel, selection.market)


@dataclass(frozen=True)
class HistoryProgress:
    selection: Selection
    message: str


class HistoricalService:
    """현재 상장 종목 전체의 선택일 거래대금을 정렬한다.

    과거 당시의 종목 구성은 확인되지 않아 상장폐지·시장 이전이 반영되지 않는다.
    이 한계를 숨기지 않고 항상 결과에 표시한다. 오늘의 순위 100개만 재정렬하지 않는다.
    """
    LIMITATION = "현재 상장 종목·현재 시장 구분 기준: 상장폐지·시장 이전으로 당시 실제 TOP100과 다를 수 있음"

    def __init__(self, cache, provider, source, clock=now_kst, spacing=0):
        self.cache = cache
        self.provider = provider
        self.source = source
        self.clock = clock
        self.spacing = spacing
        self.progress = source.progress

    def previous_day(self, target, cancel):
        return self.source.calendar.previous_day(target, cancel)

    def universe(self, market, cancel):
        return self.source.masters.universe(market, cancel)

    def query(self, selection, cancel, progress_selection=None):
        """선택한 시장 전체를 확인한 뒤 TOP100을 계산한다. 실패를 거래대금 0으로 처리하지 않는다."""
        if selection.mode not in MODES:
            raise DataError("지원하지 않는 순위 기준입니다")
        progress_selection = progress_selection or selection
        target = valid_date(selection.date)
        previous = self.previous_day(target, cancel)
        items = []
        for market in selected_markets(selection.market):
            self.progress.put(HistoryProgress(progress_selection,f"{MARKETS[market]} 전체 종목 목록 확인 중…"))
            items.extend(self.universe(market,cancel))
        names = {code:(name, market) for code,name,market in items}
        quotes, errors, consecutive_errors, cache_hits = [], 0, 0, 0
        started = time.monotonic()
        with closing(daily_results(self.cache, self.provider, tuple(names), target, cancel)) as results:
            for index,(code,record,error,from_cache) in enumerate(results,1):
                check_cancel(cancel)
                cache_hits += from_cache
                if error:
                    errors += 1
                    consecutive_errors += 1
                    LOG.warning("과거 순위 %s %s 조회 실패 (%s)",target,code,type(error).__name__)
                    if getattr(error,"retry_after",0) or consecutive_errors >= 3:
                        raise DataError(f"과거 조회 중단 ({index}/{len(items)}): {error}. 받은 자료는 보존됩니다",
                                        getattr(error,"retry_after",0)) from None
                else:
                    consecutive_errors = 0
                    if record is not None:
                        name,market = names[code]
                        quotes.append(Quote(code,name,market,record.price,record.change_rate,
                                            record.trade_value,record.volume,None,open_price=record.open_price))
                elapsed = max(.01, time.monotonic()-started)
                fetched = index-cache_hits
                eta = int((len(items)-index)*elapsed/fetched) if fetched else None
                timing = f" · 남은 약 {eta}초" if eta is not None else ""
                self.progress.put(HistoryProgress(progress_selection,
                    f"{target} 일봉 확인 {index:,}/{len(items):,} · 저장자료 {cache_hits:,} · 실패 {errors}{timing}"))
        check_cancel(cancel)
        if not quotes:
            raise DataError(f"{target}에 표시할 종목 자료가 없습니다")
        quotes.sort(key=lambda q:(-q.metric(selection.mode),q.code))
        warnings = [self.LIMITATION]
        if errors:
            warnings.append(f"{errors}종목 조회 실패: 순위 불완전. 재조회하면 실패 종목을 다시 확인합니다")
        return Snapshot(selection.mode,target,self.clock(),tuple(quotes[:100]),(),tuple(warnings),
                        not errors,previous_date=previous)


@dataclass(frozen=True)
class ReferenceSelection:
    date: str
    codes: tuple[str, ...]
    sector_day: str = ""


@dataclass(frozen=True)
class ReferenceProgress:
    date: str
    code: str
    record: DailyRecord | None
    status: str
    sector: str | None = None


@dataclass(frozen=True)
class ReferenceBatch:
    date: str
    retry_after: int = 0


class ReferenceService:
    """현재 TOP100에 필요한 과거 자료를 제한된 병렬 요청으로 조회한다. 실시간 작업은 기다리지 않는다.

    과거 캐시가 있으면 네트워크를 사용하지 않는다. 진행 결과는 큐로 즉시 전달하고,
    연속 실패나 시간 초과 시 잠시 멈춰 서버에 반복 요청이 쌓이지 않게 한다.
    """
    def __init__(self, cache, provider, sector_provider, progress=None, spacing=0):
        self.cache = cache
        self.provider = provider
        self.progress = progress if progress is not None else queue.Queue()
        self.spacing = spacing
        self.sector_provider = sector_provider
        self.sector_not_before = 0

    def query(self, selection, cancel):
        """비교일 자료를 먼저 모두 표시한 뒤 업종을 보충한다."""
        if not self.cache.is_past(selection.date):
            raise DataError("비교일은 어제 이전 날짜여야 합니다")
        started = time.monotonic()
        sectors = self.cache.load_sectors()
        records, statuses = {}, {}
        retry_after, consecutive_errors = 0, 0
        with closing(daily_results(self.cache,self.provider,selection.codes,selection.date,cancel)) as results:
            for code,record,error,_ in results:
                check_cancel(cancel)
                status = "완료" if record else "해당일 자료 없음"
                if error:
                    status = f"참고 조회 실패: {error}"
                    retry_after = max(retry_after,getattr(error,"retry_after",0),60)
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                records[code],statuses[code] = record,status
                self.progress.put(ReferenceProgress(selection.date,code,record,status,sectors.get(code)))
                if error and (getattr(error,"retry_after",0) or consecutive_errors >= 3):
                    return ReferenceBatch(selection.date,retry_after)
                if time.monotonic()-started > 45:
                    return ReferenceBatch(selection.date,max(retry_after,5))
        remaining = math.ceil(self.sector_not_before-time.monotonic())
        if remaining > 0:
            return ReferenceBatch(selection.date,max(retry_after,remaining))
        missing = [code for code in selection.codes if code not in sectors]
        buffered = []
        try:
            operation = lambda code,signal: self.sector_provider.fetch(code,signal)
            with closing(parallel_results(missing,operation,cancel)) as results:
                for code,sector,error in results:
                    check_cancel(cancel)
                    if error:
                        delay = max(60,getattr(error,"retry_after",0))
                        self.sector_not_before = time.monotonic()+delay
                        self.progress.put(ReferenceProgress(selection.date,code,records.get(code),statuses[code],"조회 실패"))
                        return ReferenceBatch(selection.date,max(retry_after,delay))
                    buffered.append((code,sector))
                    if len(buffered) >= 24:
                        self.cache.save_sectors(buffered)
                        buffered.clear()
                    self.progress.put(ReferenceProgress(selection.date,code,records.get(code),statuses[code],sector))
                    if time.monotonic()-started > 45:
                        return ReferenceBatch(selection.date,max(retry_after,5))
        finally:
            if buffered:
                self.cache.save_sectors(buffered)
        return ReferenceBatch(selection.date,retry_after)


def percentage(current, previous):
    """분모가 없거나 0인 비교를 0%로 위장하지 않는다."""
    if current is None or previous is None or previous <= 0:
        return None
    return (Decimal(current) - Decimal(previous)) / Decimal(previous) * 100


def comparison_row(rank, quote, previous):
    """오늘·과거 모두 같은 공식으로 같은 종목의 직전 거래일 대금/거래량과 비교한다."""
    value = previous.trade_value if previous else None
    volume = previous.volume if previous else None
    return {"rank":rank,"name":quote.name,"code":quote.code,"market":MARKETS[quote.market],
            "trade_value":quote.trade_value,"previous_value":value,
            "value_change":quote.trade_value-value if quote.trade_value is not None and value is not None else None,
            "value_rate":percentage(quote.trade_value,value),"open_price":quote.open_price,
            "price":quote.price,"change_rate":quote.change_rate,"volume":quote.volume,
            "volume_change":quote.volume-volume if quote.volume is not None and volume is not None else None,
            "volume_rate":percentage(quote.volume,volume)}


def format_cell(key, value):
    if value is None:
        return "—"
    if key in ("trade_value","previous_value"):
        return format_won(value)
    if key == "value_change":
        return ("+" if value>0 else "−" if value<0 else "")+format_won(abs(value))
    if key in ("value_rate","volume_rate","change_rate"):
        return f"{value:+.2f}%"
    if key == "volume_change":
        return f"{value:+,}"
    if key in ("open_price","price","volume"):
        return f"{value:,}"
    return value



