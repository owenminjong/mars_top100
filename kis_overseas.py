"""KIS 해외주식(미국) 순위 조회. 국내 시세·분봉·수급 로직과 분리된 읽기 전용 부가기능.

- 거래대금순위 HHDFS76320010 / 거래량순위 HHDFS76310010를 거래소별(NAS·NYS·AMS)로 한 번씩 조회하고
  이 PC에서 합산해 TOP100을 만든다. 금액은 달러(USD)다.
- 거래대금은 응답의 tamt를 그대로 쓴다. 현재가×거래량으로 추정하지 않는다.
- 응답에 종목 구분(주식/ETF)이 없어 ETF·ETN도 포함된다.
- 받은 자료는 파일·DB로 저장하지 않는다.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import re
import time

import mars_core as core

TRADE_VALUE = "/uapi/overseas-stock/v1/ranking/trade-pbmn"
TRADE_VOLUME = "/uapi/overseas-stock/v1/ranking/trade-vol"
UPDOWN_RATE = "/uapi/overseas-stock/v1/ranking/updown-rate"
DAILY_PRICE = "/uapi/overseas-price/v1/quotations/dailyprice"
EXCHANGES = ("NAS", "NYS", "AMS")
US_MARKETS = {"NAS": "나스닥", "NYS": "뉴욕", "AMS": "아멕스"}
LIMIT = 100
PAGE_ROWS = 100       # 거래소 한 번 호출의 최대 행 수(이보다 적으면 잘렸는지 확인할 수 없다)
MAX_ROWS = 200
DETAIL_BATCH = 20


def format_usd(value):
    """달러 금액을 억/조 단위로 표시한다. 계산은 원값 그대로, 표시만 반올림."""
    value = core.number(value)
    if value is None or value < 0:
        return "확인 불가"
    if value >= 1_000_000_000_000:
        return f"{value / Decimal(1_000_000_000_000):,.3f}조 달러"
    if value >= 100_000_000:
        return f"{value / Decimal(100_000_000):,.2f}억 달러"
    if value >= 10_000:
        return f"{value / Decimal(10_000):,.1f}만 달러"
    return f"{value:,.0f} 달러"


def eastern_now(now=None):
    """미국 동부 시각. Windows에서 tzdata가 없어도 동작하도록 서머타임 규칙을 직접 계산한다."""
    utc = (now or core.now_kst()).astimezone(dt.timezone.utc)

    def nth_sunday(month, nth):
        first = dt.date(utc.year, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))

    # 3월 둘째 일요일 02:00 EST(=07:00 UTC) ~ 11월 첫째 일요일 02:00 EDT(=06:00 UTC)
    start = dt.datetime.combine(nth_sunday(3, 2), dt.time(7, 0), tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(nth_sunday(11, 1), dt.time(6, 0), tzinfo=dt.timezone.utc)
    offset = -4 if start <= utc < end else -5
    return utc.astimezone(dt.timezone(dt.timedelta(hours=offset), "ET"))


def active_window(now=None):
    """미국 평일 프리마켓 전후부터 애프터마켓(03:00~20:00 ET) 자동 갱신. 공휴일은 판단하지 않는다."""
    et = eastern_now(now)
    return et.weekday() < 5 and 3 * 60 <= et.hour * 60 + et.minute < 20 * 60


def weekend(now=None):
    return eastern_now(now).weekday() >= 5


def clean(row, key):
    value = row.get(key)
    return " ".join(value.split()) if isinstance(value, str) else ""


def symbol(row):
    code = clean(row, "symb").upper()
    if not code:
        rsym = clean(row, "rsym").upper()  # 실시간 조회용 키: 'D' + 거래소 3자 + 심볼
        code = rsym[4:] if len(rsym) > 4 and rsym[0] == "D" else rsym
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9./\- ]{0,14}", code):
        raise core.DataError("KIS 해외 순위의 종목 심볼이 올바르지 않습니다")
    return code


def whole(row, key, required):
    value = core.number(row.get(key))
    if value is None or value < 0:
        if required:
            raise core.DataError(f"KIS 해외 순위 {key} 값이 없거나 유효하지 않습니다")
        return None
    return int(value.to_integral_value())


def change_rate(row):
    rate = core.number(row.get("rate"))
    sign = str(row.get("sign", "")).strip()
    if rate is None or sign not in ("1", "2", "3", "4", "5"):
        return rate
    return -abs(rate) if sign in ("4", "5") else Decimal(0) if sign == "3" else abs(rate)


class Page:
    """한 거래소의 파싱 결과. bad는 건너뛴 행 수, rows는 받은 행 수."""
    def __init__(self, quotes, bad, rows):
        self.quotes, self.bad, self.rows = quotes, bad, rows


def parse_page(payload, excd, mode):
    rows = payload.get("output2") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise core.DataError("KIS 해외 순위 응답 형식이 잘못되었습니다")
    if len(rows) > MAX_ROWS:
        raise core.DataError("KIS 해외 순위 응답 건수가 제한을 초과했습니다")
    quotes, seen, bad, count = [], set(), 0, 0
    for row in rows:
        if all(v in (None, "") for v in row.values()):
            continue
        count += 1
        try:
            code = symbol(row)
            if code in seen:
                raise core.DataError("KIS 해외 순위에 중복 종목이 있습니다")
            price = core.number(row.get("last"))
            if price is None or price < 0:
                raise core.DataError("KIS 해외 순위 현재가가 유효하지 않습니다")
            value = whole(row, "tamt", required=False)
            volume = whole(row, "tvol", required=True)
            if value is None and volume is not None:
                value = int((price * volume).to_integral_value())
            name = clean(row, "name") or clean(row, "ename") or code
            opened = core.number(row.get("open")) or core.number(row.get("open_price"))
            quotes.append(core.Quote(code, name[:60], excd, price, change_rate(row), value, volume, None,
                                     open_price=opened))
            seen.add(code)
        except core.DataError:
            bad += 1
    return Page(quotes, bad, count)


def request_params(excd):
    # NDAY=0: 당일, VOL_RANG=0: 전체 거래량, PRC1/PRC2 비움: 가격 제한 없음
    return {"KEYB": "", "AUTH": "", "EXCD": excd, "NDAY": "0", "PRC1": "", "PRC2": "", "VOL_RANG": "0"}


def build_snapshot(pages, mode, received):
    quotes = [q for page in pages.values() for q in page.quotes]
    if not quotes:
        raise core.DataError("표시할 미국 시세가 없습니다")
    quotes.sort(key=lambda q: (-(q.metric(mode) or 0), q.code))
    top = tuple(quotes[:LIMIT])
    warnings = ["나스닥·뉴욕·아멕스 순위를 합산 · ETF 등 종목 구분 없이 포함 · 금액은 달러 · 시세 지연 여부는 KIS 계정의 해외시세 신청 상태에 따릅니다"]
    complete = True
    bad = sum(page.bad for page in pages.values())
    if bad:
        complete = False
        warnings.append(f"응답 오류 {bad}건: 순위 불완전")
    if len(top) == LIMIT:
        cutoff = top[-1].metric(mode) or 0
        for excd, page in pages.items():
            values = [q.metric(mode) or 0 for q in page.quotes]
            if page.rows < PAGE_ROWS and values and min(values) > cutoff:
                complete = False
                warnings.append(f"{US_MARKETS[excd]} 응답이 {page.rows}건뿐이라 일부 종목이 빠졌을 수 있습니다")
    else:
        warnings.append(f"수신 종목이 {len(top)}개뿐입니다")
    return core.Snapshot(mode, received.date().isoformat(), received, top, (), tuple(warnings),
                         complete=complete, previous_date="")


class USProvider:
    """미국 TOP100. 거래소 3곳을 차례로 조회하며 공용 호출 제한·토큰·재시도를 그대로 쓴다."""
    def __init__(self, client, clock=core.now_kst):
        self.client, self.clock = client, clock
        self.rank_daily_day = None
        self.rank_daily_cache = {}
        self.surge_daily_day = None
        self.surge_daily_cache = {}
        self.surge_daily_retry_at = {}

    def fetch(self, mode, cancel):
        if mode not in core.MODES:
            raise core.DataError("지원하지 않는 순위 기준입니다")
        path = TRADE_VALUE if mode == "value" else TRADE_VOLUME
        pages = {}
        for excd in EXCHANGES:
            core.check_cancel(cancel)
            payload = self.client.get(path, request_params(excd), cancel)
            pages[excd] = parse_page(payload, excd, mode)
        core.check_cancel(cancel)
        snapshot = build_snapshot(pages, mode, self.clock())
        return self._supplement_top(snapshot, cancel)

    def _supplement_top(self, snapshot, cancel):
        """KIS 순위에 없는 시가·전일 비교값을 종목별 일봉으로 캐시 보충한다."""
        now = self.clock()
        session_day = eastern_now(now).date().isoformat()
        if session_day != self.rank_daily_day:
            self.rank_daily_day = session_day
            self.rank_daily_cache.clear()
        et = eastern_now(now)
        minute = et.hour * 60 + et.minute
        open_should_exist = et.weekday() < 5 and minute >= 9 * 60 + 30

        missing = []
        for quote in snapshot.quotes:
            cached = self.rank_daily_cache.get((quote.market, quote.code))
            if cached is None or (open_should_exist and cached[0] is None):
                missing.append(quote)
        for quote in missing[:DETAIL_BATCH]:
            core.check_cancel(cancel)
            try:
                daily = self.client.get(DAILY_PRICE, {
                    "AUTH": "", "EXCD": quote.market, "SYMB": quote.code,
                    "GUBN": "0", "BYMD": "", "MODP": "1", "KEYB": "",
                }, cancel)
            except core.Cancelled:
                raise
            except core.DataError:
                continue
            bars = daily.get("output2") if isinstance(daily, dict) else None
            bars = bars if isinstance(bars, list) else []
            dated = [(clean(bar, "xymd"), bar) for bar in bars
                     if isinstance(bar, dict) and re.fullmatch(r"\d{8}", clean(bar, "xymd"))]
            today = next((bar for day, bar in dated if day == session_day), None)
            previous_day = max((day for day, _ in dated if day < session_day), default=None)
            previous = next((bar for day, bar in dated if day == previous_day), None)
            opened = core.number(today.get("open")) if today else None
            previous_value = whole(previous, "tamt", required=False) if previous else None
            previous_volume = whole(previous, "tvol", required=False) if previous else None
            if previous_value is None and previous and previous_volume is not None:
                close = core.number(previous.get("clos"))
                if close is not None:
                    previous_value = int((close * previous_volume).to_integral_value())
            self.rank_daily_cache[(quote.market, quote.code)] = (opened, previous_value, previous_volume)

        enriched = []
        for quote in snapshot.quotes:
            opened, previous_value, previous_volume = self.rank_daily_cache.get(
                (quote.market, quote.code), (None, None, None))
            value = quote.trade_value
            if value is None and quote.volume is not None:
                # 거래량 순위에 포함된 종목의 거래대금 순위 응답이 없으면 추정치를 쓴다.
                value = int((quote.price * quote.volume).to_integral_value())
            enriched.append(core.Quote(quote.code, quote.name, quote.market, quote.price,
                                       quote.change_rate, value, quote.volume, quote.as_of,
                                       quote.sector, quote.open_price if quote.open_price is not None else opened,
                                       previous_value, previous_volume))
        warnings = list(snapshot.warnings)
        if missing:
            warnings.append(f"시가·전거래일 비교 보충 {min(DETAIL_BATCH, len(missing))}/{len(missing)}종목 처리 · 다음 자동 갱신에서 이어서 조회")
        if snapshot.mode == "volume":
            warnings.append("거래량 순위의 거래대금 응답이 없는 종목은 현재가×거래량 추정값")
        return core.Snapshot(snapshot.mode, snapshot.data_date, snapshot.received_at,
                             tuple(enriched), snapshot.statuses, tuple(warnings),
                             snapshot.complete, snapshot.retry_after, snapshot.previous_date)

    def fetch_surge(self, cancel):
        """나스닥·뉴욕·아멕스의 장중 상승률 상위 종목과 비교 일봉을 조회한다."""
        received = self.clock()
        session_day = eastern_now(received).date().isoformat()
        if session_day != self.surge_daily_day:
            self.surge_daily_day = session_day
            self.surge_daily_cache.clear()
            self.surge_daily_retry_at.clear()
        quotes, seen = [], set()
        for excd in EXCHANGES:
            core.check_cancel(cancel)
            payload = self.client.get(UPDOWN_RATE,
                                      {"AUTH": "", "EXCD": excd, "GUBN": "1", "NDAY": "0",
                                       "VOL_RANG": "0", "KEYB": ""}, cancel)
            rows = payload.get("output2") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise core.DataError("KIS 해외 상승률 응답 형식이 잘못되었습니다")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    code = symbol(row)
                    rate = change_rate(row)
                    price = core.number(row.get("last"))
                    volume = whole(row, "tvol", required=False)
                    current_value = whole(row, "tamt", required=False)
                    opening = core.number(row.get("open")) or core.number(row.get("open_price"))
                    if rate is None or rate <= 0 or price is None or price < 0:
                        continue
                    key = (excd, code)
                    if key in seen:
                        continue
                    seen.add(key)
                    name = clean(row, "name") or clean(row, "ename") or code
                    quotes.append(core.Quote(code, name[:60], excd, price, rate, current_value, volume, None, open_price=opening))
                except core.DataError:
                    continue
        core.check_cancel(cancel)
        if not quotes:
            raise core.DataError("표시할 미국 상승 종목이 없습니다")
        trade_rank = {}
        for excd in EXCHANGES:
            core.check_cancel(cancel)
            ranked = self.client.get(TRADE_VALUE, request_params(excd), cancel)
            ranked_rows = ranked.get("output2") if isinstance(ranked, dict) else None
            if not isinstance(ranked_rows, list):
                continue
            for item in ranked_rows:
                if not isinstance(item, dict):
                    continue
                try:
                    item_code = symbol(item)
                    trade_rank[(excd, item_code)] = (
                        whole(item, "tamt", required=False), whole(item, "tvol", required=False))
                except core.DataError:
                    continue
        previous_trade_rank = {}
        for excd in EXCHANGES:
            core.check_cancel(cancel)
            params = request_params(excd)
            params["NDAY"] = "1"
            ranked = self.client.get(TRADE_VALUE, params, cancel)
            ranked_rows = ranked.get("output2") if isinstance(ranked, dict) else None
            if not isinstance(ranked_rows, list):
                continue
            for item in ranked_rows:
                if not isinstance(item, dict):
                    continue
                try:
                    item_code = symbol(item)
                    previous_trade_rank[(excd, item_code)] = (
                        whole(item, "tamt", required=False), whole(item, "tvol", required=False))
                except core.DataError:
                    continue
        quotes.sort(key=lambda q: (-(q.change_rate or 0), q.code, q.market))

        # 일봉은 종목별 호출이므로 새 종목과 누락된 상세값을 조회해 캐시한다.
        # 순위 응답은 기다리지 않고 매 주기마다 최신 상태를 계속 반환한다.
        details_left = LIMIT
        for quote in quotes[:LIMIT]:
            key = (quote.market, quote.code)
            cached = self.surge_daily_cache.get(key)
            if (cached is not None and all(value is not None for value in cached)) or details_left <= 0:
                continue
            retry_at = self.surge_daily_retry_at.get(key, 0.0)
            if time.monotonic() < retry_at:
                continue
            core.check_cancel(cancel)
            details_left -= 1
            try:
                daily = self.client.get(DAILY_PRICE, {
                    "AUTH": "", "EXCD": quote.market, "SYMB": quote.code,
                    "GUBN": "0", "BYMD": "", "MODP": "1", "KEYB": "",
                }, cancel)
            except core.Cancelled:
                raise
            except core.DataError as exc:
                # 한 종목의 상품별 일봉 오류가 나머지 상승 종목 조회를 막지 않게 잠시 제외한다.
                self.surge_daily_retry_at[key] = time.monotonic() + max(15, getattr(exc, "retry_after", 0) or 0)
                continue
            self.surge_daily_retry_at.pop(key, None)
            summary = daily.get("output1") if isinstance(daily, dict) else None
            if isinstance(summary, list):
                summary = summary[0] if summary else None
            bars = daily.get("output2") if isinstance(daily, dict) else None
            if not isinstance(bars, list):
                bars = []
            dated_bars = []
            for bar in bars:
                if not isinstance(bar, dict):
                    continue
                day = clean(bar, "xymd")
                if re.fullmatch(r"\d{8}", day) and day <= session_day:
                    dated_bars.append((day, bar))
            today_bar = next((bar for day, bar in dated_bars if day == session_day), None)
            if today_bar is None and dated_bars:
                # 휴장·주말·장전에는 가장 최근 거래일 봉을 현재 기준 봉으로 사용한다.
                today_day, today_bar = max(dated_bars, key=lambda item: item[0])
                previous_candidates = [(day, bar) for day, bar in dated_bars if day < today_day]
                previous_bar = max(previous_candidates, key=lambda item: item[0])[1] if previous_candidates else None
            else:
                previous_candidates = [(day, bar) for day, bar in dated_bars if day < session_day]
                previous_bar = max(previous_candidates, key=lambda item: item[0])[1] if previous_candidates else None
            opened = core.number(today_bar.get("open")) if today_bar else None
            if opened is None and isinstance(summary, dict):
                opened = core.number(summary.get("open"))
            if opened is None:
                opened = quote.open_price
            current_value = whole(today_bar, "tamt", required=False) if today_bar else None
            if current_value is None and isinstance(summary, dict):
                current_value = whole(summary, "tamt", required=False)
            previous_value = whole(previous_bar, "tamt", required=False) if previous_bar else None
            previous_volume = whole(previous_bar, "tvol", required=False) if previous_bar else None
            if previous_value is None and previous_bar and previous_volume is not None:
                previous_close = core.number(previous_bar.get("clos"))
                if previous_close is not None:
                    previous_value = int((previous_close * previous_volume).to_integral_value())
            today_volume = whole(today_bar, "tvol", required=False) if today_bar else None
            if today_volume is None and isinstance(summary, dict):
                today_volume = whole(summary, "tvol", required=False)
            self.surge_daily_cache[key] = (opened, previous_value, previous_volume, current_value, today_volume)

        enriched = []
        for quote in quotes[:LIMIT]:
            cached = self.surge_daily_cache.get((quote.market, quote.code))
            opened, previous_value, previous_volume, daily_value, daily_volume = cached or (None, None, None, None, None)
            ranked_value, ranked_volume = trade_rank.get((quote.market, quote.code), (None, None))
            ranked_previous_value, ranked_previous_volume = previous_trade_rank.get((quote.market, quote.code), (None, None))
            previous_value = quote.previous_trade_value if quote.previous_trade_value is not None else ranked_previous_value if ranked_previous_value is not None else previous_value
            previous_volume = quote.previous_volume if quote.previous_volume is not None else ranked_previous_volume if ranked_previous_volume is not None else previous_volume
            volume = quote.volume if quote.volume is not None else ranked_volume if ranked_volume is not None else daily_volume
            # 순위 API 거래대금이 있으면 우선 사용하고, 없으면 가격×누적거래량으로 추정한다.
            current_value = quote.trade_value if quote.trade_value is not None else ranked_value
            if current_value is None and volume is not None:
                current_value = int((quote.price * volume).to_integral_value())
            if current_value is None:
                current_value = daily_value
            enriched.append(core.Quote(quote.code, quote.name, quote.market, quote.price,
                                       quote.change_rate, current_value, volume, quote.as_of,
                                       quote.sector, opened, previous_value, previous_volume))
        return core.Snapshot("value", received.date().isoformat(), received,
                             tuple(enriched), (),
                             ("미국 상승률 상위 합산 · 일봉 보충은 종목별 캐시 · 누적 거래대금은 필요한 경우 현재가×거래량 추정",),
                             complete=True, previous_date="")

















