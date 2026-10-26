"""KRX same-minute comparisons. Public minute data lives only in a window's memory.

KIS FHKST03010230 output2.acml_tr_pbmn is cumulative KRW, NOT a bar amount.
Never sum cumulative observations or estimate traded value from OHLC * volume.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import queue
import re
import threading
import time

import mars_core as core
from kis_provider import KISCalendar, kis_date, required_int, response_rows

MINUTES = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
OPEN, CLOSE = 9 * 60, 15 * 60 + 30


def minute_text(minute):
    return f"{minute // 60:02}:{minute % 60:02}"


def read_minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise core.DataError("시각은 HH:MM 형식으로 입력하세요")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59 or not OPEN <= hour * 60 + minute <= CLOSE:
        raise core.DataError("완료 분봉 시각은 09:00~15:30 사이로 입력하세요")
    return hour * 60 + minute


@dataclass(frozen=True)
class Moment:
    day: str
    previous: str
    minute: int
    note: str = ""


def resolve_moment(calendar, date, minute, cancel, now):
    """Freeze a common completed minute BEFORE starting any stock requests."""
    today = now.date().isoformat()
    if date is None:
        day, previous = calendar.latest(cancel)
        if day > today or previous >= day:
            raise core.DataError("KIS 거래일 순서가 잘못되었습니다")
        if day < today:
            return Moment(day, previous, CLOSE, "오늘 거래 기록 없음 · 최근 거래일 종가 분봉 기준")
        cutoff = min(CLOSE, now.hour * 60 + now.minute - 1)
        if cutoff < OPEN:
            raise core.DataError("아직 완료된 정규장 분봉이 없습니다. 09:01 이후 다시 조회하세요")
        return Moment(day, previous, cutoff, "조회 시작 시각에 완료된 분봉으로 고정")
    date = core.valid_date(date)
    minute = read_minute(minute)
    if date > today:
        raise core.DataError("미래 날짜는 조회할 수 없습니다")
    if date == today and minute >= now.hour * 60 + now.minute:
        raise core.DataError("진행 중이거나 미래인 분봉은 비교할 수 없습니다. 완료된 분을 선택하세요")
    dates = calendar.dates(date, cancel)
    if len(dates) < 2 or dates[0] != date:
        raise core.DataError(f"{date}은 휴장일이거나 KIS 거래 기록이 없습니다")
    return Moment(date, dates[1], minute)


@dataclass(frozen=True)
class Bar:
    minute: int
    amount: int


def parse_bars(payload, day, end):
    """Use dated output2 only; output1 can describe a different/latest session."""
    rows = response_rows(payload, "output2")
    if len(rows) > 120:
        raise core.DataError("KIS 분봉 응답 건수가 제한을 초과했습니다")
    bars = {}
    for row in rows:
        if all(v in (None, "") for v in row.values()):
            continue
        if kis_date(row.get("stck_bsop_date")) != day:
            raise core.DataError("KIS 분봉 거래일이 요청 날짜와 다릅니다")
        stamp = row.get("stck_cntg_hour", "")
        if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{4}00", stamp):
            raise core.DataError("KIS 분봉 시각 형식이 잘못되었습니다")
        hour, minute = int(stamp[:2]), int(stamp[2:4])
        if hour > 23 or minute > 59:
            raise core.DataError("KIS 분봉 시각이 유효하지 않습니다")
        index = hour * 60 + minute
        # Both days use KRX (J) and the same fixed 09:00..15:30 window.
        if index < OPEN or index > CLOSE:
            continue
        if index > end:
            raise core.DataError("KIS가 요청 시각보다 미래의 분봉을 반환했습니다")
        amount = required_int(row, "acml_tr_pbmn")
        if index in bars:
            raise core.DataError("KIS 분봉 시각이 중복되었습니다")
        bars[index] = amount
    ordered = tuple(Bar(m, v) for m, v in sorted(bars.items()))
    if any(a.amount > b.amount for a, b in zip(ordered, ordered[1:])):
        raise core.DataError("KIS 누적 거래대금이 시간 순서와 맞지 않습니다")
    return ordered


class MinuteProvider:
    """A separate 6/sec ceiling reserves room for the existing main view.

    All workers also share KISClient's global token lock and rate limiter.
    """
    def __init__(self, client, interval=1/6):
        self.client, self.interval = client, interval
        self.lock = threading.Lock()
        self.next_call = 0.0

    def page(self, code, day, end, cancel):
        code = core.stock_code({"code": code})
        day = core.valid_date(day)
        while True:
            core.check_cancel(cancel)
            with self.lock:
                wait = self.next_call - time.monotonic()
                if wait <= 0:
                    self.next_call = time.monotonic() + self.interval
                    break
            if cancel.wait(min(wait, .1)):
                raise core.Cancelled()
        data = self.client.get(MINUTES, {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": day.replace("-", ""),
            "FID_INPUT_HOUR_1": minute_text(end).replace(":", "") + "00",
            "FID_PW_DATA_INCU_YN": "N", "FID_FAKE_TICK_INCU_YN": ""}, cancel)
        core.check_cancel(cancel)
        return parse_bars(data, day, end)


class MinuteBook:
    """One selected stock's two dates, retained in memory until it changes or closes."""
    def __init__(self, provider):
        self.provider = provider
        self.pages = {}
        self.series_cache = {}
        self.lock = threading.RLock()

    def page(self, code, day, end, cancel):
        core.check_cancel(cancel)
        key = (code, day, end)
        with self.lock:
            if key in self.pages:
                return self.pages[key]
        bars = self.provider.page(code, day, end, cancel)
        core.check_cancel(cancel)
        with self.lock:
            self.pages[key] = bars
        return bars

    def at(self, code, day, minute, cancel):
        core.check_cancel(cancel)
        with self.lock:
            cached = self.series_cache.get((code, day))
        if cached and cached[0] >= minute:
            return cached[1].get(minute) if OPEN in cached[1] else None
        bars = self.page(code, day, minute, cancel)
        value = next((b.amount for b in bars if b.minute == minute), None)
        if value is None:
            return None
        # Do not compare a delayed opening/new listing's shorter session to 09:00.
        # Reuse the endpoint page if it already reaches the opening minute.
        if not any(b.minute == OPEN for b in bars):
            opening = self.page(code, day, OPEN, cancel)
            if not any(b.minute == OPEN for b in opening):
                return None
            if opening[-1].amount > bars[0].amount:
                raise core.DataError("개장 분봉과 조회 시각의 누적 거래대금이 역전되었습니다")
        return value

    def series(self, code, day, end, cancel):
        """Walk backwards by the actual oldest row, never by assumed page length."""
        core.check_cancel(cancel)
        with self.lock:
            cached = self.series_cache.get((code, day))
        if cached and cached[0] >= end:
            return {m: v for m, v in cached[1].items() if m <= end}
        cursor, found = end, {}
        for _ in range(CLOSE - OPEN + 1):
            bars = self.page(code, day, cursor, cancel)
            if not bars:
                break
            for bar in bars:
                if bar.minute in found and found[bar.minute] != bar.amount:
                    raise core.DataError("페이지 사이의 KIS 분봉 값이 일치하지 않습니다")
                found[bar.minute] = bar.amount
            if cached and bars[0].minute <= cached[0]:
                for minute, amount in cached[1].items():
                    if minute in found and found[minute] != amount:
                        raise core.DataError("완료된 분봉의 누적 거래대금이 변경되었습니다")
                    found.setdefault(minute, amount)
                break
            if bars[0].minute <= OPEN:
                break
            # Even a short page may have earlier data: request to the boundary explicitly.
            next_cursor = bars[0].minute - 1
            if next_cursor >= cursor:
                raise core.DataError("KIS 분봉 페이지가 이전 시각으로 진행되지 않습니다")
            cursor = next_cursor
        ordered = sorted(found.items())
        if any(a[1] > b[1] for a, b in zip(ordered, ordered[1:])):
            raise core.DataError("페이지 사이의 누적 거래대금이 역전되었습니다")
        with self.lock:
            self.series_cache[(code, day)] = (end, dict(found))
        return found


@dataclass(frozen=True)
class CompareRequest:
    serial: int
    quotes: tuple
    date: str | None = None
    time: str = ""


@dataclass(frozen=True)
class Comparison:
    code: str
    current: int | None
    previous: int | None
    status: str

    @property
    def difference(self):
        if self.current is None or self.previous is None:
            return None
        return self.current - self.previous

    @property
    def rate(self):
        return core.percentage(self.current, self.previous)


@dataclass(frozen=True)
class Session:
    request: CompareRequest
    moment: Moment
    book: MinuteBook


@dataclass(frozen=True)
class CompareProgress:
    serial: int
    session: Session | None = None
    comparison: Comparison | None = None


def compare_stock(book, code, moment, cancel):
    amounts, reasons = [], []
    for label, day in (("기준일", moment.day), ("전 거래일", moment.previous)):
        value = book.at(code, day, moment.minute, cancel)
        amounts.append(value)
        if value is None:
            reasons.append(f"{label} 해당 시각/09:00 분봉 없음")
    status = "비교 불가 · " + " / ".join(reasons) if reasons else (
        "증가율 없음 · 전 거래일 0원" if amounts[1] == 0 else "완료")
    return Comparison(code, *amounts, status)


class ComparisonService:
    def __init__(self, client, provider=None, calendar=None, clock=core.now_kst):
        self.provider = provider or MinuteProvider(client)
        self.calendar = calendar or KISCalendar(client, clock)
        self.clock, self.progress = clock, queue.Queue()
        self.book_key = self.book = None

    def query(self, selection, cancel):
        if len(selection.quotes) != 1:
            raise core.DataError("비교할 종목 하나를 선택하세요")
        moment = resolve_moment(self.calendar, selection.date, selection.time, cancel, self.clock())
        key = (selection.quotes[0].code, moment.day, moment.previous)
        if key != self.book_key:
            self.book_key, self.book = key, MinuteBook(self.provider)
        session = Session(selection, moment, self.book)
        self.progress.put(CompareProgress(selection.serial, session=session))
        result = compare_stock(session.book, selection.quotes[0].code, moment, cancel)
        core.check_cancel(cancel)
        self.progress.put(CompareProgress(selection.serial, comparison=result))
        return session


@dataclass(frozen=True)
class GraphRequest:
    session: Session
    code: str


@dataclass(frozen=True)
class GraphData:
    current: dict
    previous: dict
    errors: tuple = ()


class GraphService:
    def query(self, selection, cancel):
        session = selection.session
        values, errors = [], []
        for label, day in (("기준일", session.moment.day), ("전 거래일", session.moment.previous)):
            core.check_cancel(cancel)
            try:
                end = session.moment.minute if day == session.moment.day else CLOSE
                series = session.book.series(selection.code, day, end, cancel)
                if OPEN not in series:
                    values.append({})
                    errors.append(f"{label}: 09:00 분봉 없음")
                else:
                    values.append(series)
            except core.DataError as exc:
                if getattr(exc, "retry_after", 0):
                    raise
                values.append({})
                errors.append(f"{label}: {exc}")
        return GraphData(*values, tuple(errors))


def graph_points(data, end, step, current_end=None):
    """Sample cumulative observations, not sums. Missing points break the line."""
    if step not in (5, 15):
        raise ValueError("graph interval must be 5 or 15 minutes")
    ticks = list(range(OPEN, end + 1, step))
    if not ticks or ticks[-1] != end:
        ticks.append(end)
    if current_end is not None and OPEN <= current_end <= end and current_end not in ticks:
        ticks.append(current_end)
        ticks.sort()
    return [(minute, data.current.get(minute), data.previous.get(minute)) for minute in ticks]
