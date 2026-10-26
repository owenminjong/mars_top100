"""KIS KRX adapters. All rankings and comparisons are computed on this PC."""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import io
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import mars_core as core
from kis_client import NoRedirect
from kis_overseas import USProvider
from kis_storage import atomic_write

MULTI = "/uapi/domestic-stock/v1/quotations/intstock-multprice"
DAILY = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
INDEX = "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"


def kis_date(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{8}", raw):
        raise core.DataError("KIS 거래일 형식이 잘못되었습니다")
    return core.valid_date(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")


def response_rows(payload, key):
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise core.DataError("KIS 시세 응답 형식이 잘못되었습니다")
    return rows


def required_int(row, key):
    result = core.first_number(row, (key,), nonnegative=True, integral=True)
    if result is None:
        raise core.DataError(f"KIS {key} 값이 없거나 유효하지 않습니다")
    return result


def parse_master(data, market):
    """Official CP949 format: fixed ASCII suffix excludes the newline (227/221)."""
    suffix = {"KOSPI": 227, "KOSDAQ": 221}[market]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo(market.lower() + "_code.mst")
            if info.file_size > 12 * 1024 * 1024:
                raise ValueError()
            lines = z.read(info).decode("cp949").splitlines()
        items, seen = [], set()
        for row in lines:
            if len(row) <= 21 + suffix:
                raise ValueError()
            # Equities, preferred shares, REITs, funds and foreign shares/DRs.
            # ETFs/ETNs, warrants and beneficiary certificates are not individual stocks.
            group = row[-suffix:][:2]
            if group not in {"ST", "RT", "IF", "MF", "PF", "DR", "FS"}:
                continue
            code, name = row[:9].strip(), row[21:-suffix].strip()
            if not re.fullmatch(r"[0-9A-Z]{6}", code) or not name or code in seen:
                raise ValueError()
            seen.add(code)
            items.append((code, name, market))
        if not items or len(items) > 10000:
            raise ValueError()
        return tuple(items)
    except (ValueError, KeyError, UnicodeError, zipfile.BadZipFile, OSError):
        raise core.DataError("KIS 종목 마스터 형식이 변경되었거나 파일이 손상되었습니다") from None


class KISMaster:
    def __init__(self, directory=None, clock=core.now_kst):
        self.directory = Path(directory) if directory is not None else core.user_data_dir()
        self.clock = clock
        self.loaded = {}

    def universe(self, market, cancel):
        core.check_cancel(cancel)
        day = self.clock().date().isoformat()
        key = (day, market)
        if key in self.loaded:
            return self.loaded[key]
        path = self.directory / ("kis_" + market.lower() + "_master.zip")
        if path.exists() and dt.datetime.fromtimestamp(path.stat().st_mtime, core.KST).date().isoformat() == day:
            try:
                if path.stat().st_size <= 4 * 1024 * 1024:
                    self.loaded[key] = parse_master(path.read_bytes(), market)
                    return self.loaded[key]
            except core.DataError:
                pass
        url = f"https://new.real.download.dws.co.kr/common/master/{market.lower()}_code.mst.zip"
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(url, timeout=10) as response:
                chunks, size = [], 0
                deadline = time.monotonic() + 30
                while True:
                    core.check_cancel(cancel)
                    if time.monotonic() > deadline:
                        raise core.DataError("KIS 종목 목록 다운로드 시간이 초과되었습니다", 10)
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise core.DataError("KIS 종목 목록 크기가 제한을 초과했습니다")
                    chunks.append(chunk)
            data = b"".join(chunks)
            items = parse_master(data, market)
            core.check_cancel(cancel)
            atomic_write(path, data)
            self.loaded = {k: v for k, v in self.loaded.items() if k[0] == day}
            self.loaded[key] = items
            return items
        except (urllib.error.URLError, OSError):
            raise core.DataError("KIS 공식 종목 목록 다운로드 실패. 인터넷·인증서 또는 서버 점검 상태를 확인하세요", 10) from None


class KISCalendar:
    def __init__(self, client, clock=core.now_kst):
        self.client, self.clock = client, clock
        self.previous = {}

    def dates(self, end, cancel):
        end = dt.date.fromisoformat(core.valid_date(end))
        start = end - dt.timedelta(days=40)
        data = self.client.get(INDEX, {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001",
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D"}, cancel)
        dates = set()
        for row in response_rows(data, "output2"):
            if not row.get("stck_bsop_date") and all(v in (None, "") for v in row.values()):
                continue
            day = kis_date(row.get("stck_bsop_date"))
            if not start.isoformat() <= day <= end.isoformat() or day in dates:
                raise core.DataError("KIS 지수 거래일 범위가 잘못되었습니다")
            # Today's untraded placeholder is not evidence of a trading session.
            if required_int(row, "acml_vol") > 0:
                dates.add(day)
        return sorted(dates, reverse=True)

    def latest(self, cancel):
        days = self.dates(self.clock().date().isoformat(), cancel)
        if len(days) < 2:
            raise core.DataError("최근 거래일과 직전 거래일을 확인할 수 없습니다")
        return days[0], days[1]

    def previous_day(self, target, cancel):
        target = core.valid_date(target)
        if target >= self.clock().date().isoformat():
            raise core.DataError("과거 조회는 어제 이전 날짜만 가능합니다")
        if target not in self.previous:
            days = self.dates(target, cancel)
            if len(days) < 2 or days[0] != target:
                raise core.DataError(f"{target}은 휴장일이거나 KIS 거래 기록이 없습니다. 거래일을 선택하세요")
            self.previous[target] = days[1]
        return self.previous[target]


class KISHistoryProvider:
    def __init__(self, client, clock=core.now_kst):
        self.client, self.clock = client, clock

    def fetch(self, code, target_date, cancel):
        code = core.stock_code({"code": code})
        target = dt.date.fromisoformat(core.valid_date(target_date))
        if target >= self.clock().date():
            raise core.DataError("과거 조회는 어제 이전 날짜만 가능합니다")
        # One symbol per API call, but collect adjacent days together (at most 90 rows).
        # Include later cached-safe days too, so selecting yesterday after last week is cheap.
        end = min(target + dt.timedelta(days=30), self.clock().date() - dt.timedelta(days=1))
        start = end - dt.timedelta(days=89)
        data = self.client.get(DAILY, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}, cancel)
        identity = data.get("output1", {})
        if not isinstance(identity, dict) or (identity.get("stck_shrn_iscd") and identity["stck_shrn_iscd"] != code):
            raise core.DataError("KIS 일봉 종목코드가 요청과 다릅니다")
        records, seen = [], set()
        for row in response_rows(data, "output2"):
            if not row.get("stck_bsop_date") and all(v in (None, "") for v in row.values()):
                continue
            day = kis_date(row.get("stck_bsop_date"))
            if not start.isoformat() <= day <= end.isoformat() or day in seen:
                raise core.DataError("KIS 일봉 거래일 범위가 잘못되었습니다")
            seen.add(day)
            price = required_int(row, "stck_clpr")
            volume = required_int(row, "acml_vol")
            amount = required_int(row, "acml_tr_pbmn")  # KRW, never close * volume.
            opened = required_int(row, "stck_oprc")
            diff = core.number(row.get("prdy_vrss"))
            sign = row.get("prdy_vrss_sign")
            rate = None
            if diff is not None and sign in ("1", "2", "3", "4", "5"):
                diff = -abs(diff) if sign in ("4", "5") else 0 if sign == "3" else abs(diff)
                rate = core.percentage(price, price - diff)
            records.append(core.DailyRecord(code, day, price, volume, amount,
                                            open_price=opened, change_rate=rate))
        return tuple(records), (code, start.isoformat(), end.isoformat())


class KISSectorProvider:
    def __init__(self, client):
        self.client = client

    def fetch(self, code, cancel):
        code = core.stock_code({"code": code})
        payload = self.client.get(PRICE, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, cancel)
        row = payload.get("output")
        if not isinstance(row, dict):
            raise core.DataError("KIS 업종 응답 형식이 잘못되었습니다")
        if row.get("stck_shrn_iscd") and row["stck_shrn_iscd"] != code:
            raise core.DataError("KIS 업종 응답 종목코드가 다릅니다")
        return core.first_text(row, ("bstp_kor_isnm",)) or "미제공"


class KISProvider:
    def __init__(self, client, cache, masters=None, clock=core.now_kst):
        self.client, self.cache, self.clock = client, cache, clock
        self.masters = masters or KISMaster(cache.directory, clock)
        self.calendar = KISCalendar(client, clock)
        self.history = KISHistoryProvider(client, clock)
        self.sectors = KISSectorProvider(client)
        self.overseas = USProvider(client, clock)
        self.progress = queue.Queue()
        # 장중 실시간 장부: 백그라운드 순환 조회가 종목별 최신 시세를 계속 갱신한다.
        self.book, self.book_day, self.book_prev = {}, None, None
        self.book_lock = threading.Lock()
        self.sweeper, self.halt = None, threading.Event()
        self.last_used, self.hot, self.sweep_error = 0.0, (), ""
        self.book_started = 0.0

    def closed_batch_ok(self, day, cancel):
        """Weekend/holiday/pre-open fast path: 30 stocks per call instead of 1 per call.

        Only trusted if the multi-quote API for a reference stock matches that stock's
        official daily bar for `day`. Any doubt falls back to the slow per-stock path.
        """
        try:
            payload = self.client.get(MULTI, {"FID_COND_MRKT_DIV_CODE_1": "J",
                                              "FID_INPUT_ISCD_1": "005930"}, cancel)
            row = next((r for r in response_rows(payload, "output") if core.stock_code(r) == "005930"), None)
            if row is None:
                return False
            price, amount = required_int(row, "inter2_prpr"), required_int(row, "acml_tr_pbmn")
            records, _ = self.history.fetch("005930", day, cancel)
            ref = next((r for r in records if r.date == day), None)
            if ref is None or not ref.trade_value or ref.price != price:
                return False
            ok = abs(amount - ref.trade_value) <= ref.trade_value * 0.005
        except core.DataError as exc:
            if getattr(exc, "retry_after", 0):
                raise
            ok = False
        core.LOG.info("마감 일괄 조회 검증 %s: %s", day, "사용" if ok else "불일치, 종목별 조회로 전환")
        return ok

    HOT = 120            # 상위 종목: 매 바퀴 재조회 (30종목 묶음 4개를 동시에)
    COLD_PER_LAP = 3     # 나머지 종목: 한 바퀴마다 30종목 묶음 3개씩 돌아가며 재조회
    HOT_MAX_AGE = 6.0    # 화면에 쓰는 상위 종목 시세의 최대 허용 나이(초)
    COLD_MAX_AGE = 90.0  # 전체 종목 시세의 최대 허용 나이(초)
    IDLE_STOP = 15.0     # 화면이 조회를 멈추면 백그라운드 순환도 멈춘다
    PRIORITY = 300       # 첫 화면: 전일 거래대금 상위 N종목을 먼저 조회(10번 호출)
    WARM_SECONDS = 60    # 시작 직후 이 시간 동안은 나머지 종목을 더 빠르게 채운다

    def scan_batch(self, batch, signal):
        """30종목 멀티시세 한 번. 잘못된 행은 세지만 이전 값으로 대체하지 않는다."""
        expected = {code: (name, mkt) for code, name, mkt in batch}
        params = {}
        for i, (code, _, _) in enumerate(batch, 1):
            params[f"FID_COND_MRKT_DIV_CODE_{i}"] = "J"
            params[f"FID_INPUT_ISCD_{i}"] = code
        payload = self.client.get(MULTI, params, signal)
        rows = response_rows(payload, "output")
        if len(rows) > 30:
            raise core.DataError("KIS 멀티종목 응답 수가 잘못되었습니다")
        found, bad, seen = [], 0, set()
        for row in rows:
            try:
                code = core.stock_code(row)
                if code not in expected or code in seen:
                    raise core.DataError("KIS 멀티종목 응답에 중복·미요청 종목이 있습니다")
                name, mkt = expected[code]
                price = required_int(row, "inter2_prpr")
                amount = required_int(row, "acml_tr_pbmn")
                volume = required_int(row, "acml_vol")
                opened = required_int(row, "inter2_oprc")
                found.append(core.Quote(code, name, mkt, price, core.number(row.get("prdy_ctrt")),
                                        amount, volume, None, open_price=opened))
                seen.add(code)
            except core.DataError:
                bad += 1
        return found, bad + len(expected.keys() - seen), time.monotonic()

    def scan_items(self, items, selection, cancel, report=True, workers=4):
        """전체 조회(첫 화면용): 묶음을 동시에 요청한다. 시작 간격은 공용 throttle이 지킨다."""
        quotes, errors, stamps = [], 0, {}
        batches = [tuple(items[offset:offset + 30]) for offset in range(0, len(items), 30)]
        done = 0
        with closing(core.parallel_results(batches, self.scan_batch, cancel, workers=workers)) as results:
            for _batch, value, error in results:
                core.check_cancel(cancel)
                if error:
                    raise error
                found, bad, stamp = value
                quotes.extend(found)
                stamps.update({q.code: stamp for q in found})
                errors += bad
                done += 1
                if report:
                    self.progress.put(core.HistoryProgress(selection,
                        f"KIS 전체 시세 확인 {min(done * 30, len(items)):,}/{len(items):,}종목"))
        return quotes, errors, stamps

    def store(self, quotes, day, stamps=None):
        now = time.monotonic()
        with self.book_lock:
            if self.book_day != day:
                self.book, self.book_day, self.book_started = {}, day, now
            for quote in quotes:
                self.book[quote.code] = (quote, (stamps or {}).get(quote.code, now))

    def ensure_sweeper(self, day):
        self.last_used = time.monotonic()
        if self.sweeper is not None and self.sweeper.is_alive():
            return
        self.halt = threading.Event()
        self.sweeper = threading.Thread(target=self.sweep, args=(day, self.halt),
                                        name="kis-sweeper", daemon=True)
        self.sweeper.start()

    def stop_sweeper(self):
        self.halt.set()

    def sweep(self, day, halt):
        """상위 종목은 매 바퀴, 나머지는 돌아가며 계속 다시 받는다. 실패는 대기 후 자동 재시도."""
        try:
            universe = []
            for market in core.MARKETS:
                universe.extend(self.masters.universe(market, halt))
        except (core.DataError, core.Cancelled):
            return
        batches = [tuple(universe[i:i + 30]) for i in range(0, len(universe), 30)]
        cursor, failures = 0, 0
        while not halt.is_set():
            if time.monotonic() - self.last_used > self.IDLE_STOP or self.clock().date().isoformat() != day:
                break
            hot = self.hot
            work = [tuple(hot[i:i + 30]) for i in range(0, len(hot), 30)]
            for _ in range(8 if self.warming() else self.COLD_PER_LAP):
                work.append(batches[cursor % len(batches)])
                cursor += 1
            got, stamps, failed, wait = [], {}, False, 0.0
            try:
                with closing(core.parallel_results(work, self.scan_batch, halt, workers=4)) as results:
                    for _batch, value, error in results:
                        if error is not None:
                            failed = True
                            self.sweep_error = str(error)
                            wait = max(wait, min(10, getattr(error, "retry_after", 0) or 0))
                        else:
                            got.extend(value[0])
                            stamps.update({q.code: value[2] for q in value[0]})
            except core.Cancelled:
                break
            except Exception as exc:  # 예상 밖 오류로 순환 조회가 조용히 멈추지 않게 한다
                core.LOG.warning("순환 조회 오류: %s", type(exc).__name__)
                self.sweep_error, failed = type(exc).__name__, True
            if got:
                self.store(got, day, stamps)
            failures = failures + 1 if failed else 0
            if failed and halt.wait(max(wait, min(2.0, 0.2 * 2 ** min(failures, 4)))):
                break

    def warming(self):
        return time.monotonic() - self.book_started < self.WARM_SECONDS

    def priority_items(self, items, previous):
        """전일 거래대금 상위 종목을 먼저 조회해 첫 화면을 빠르게 만든다. 저장 자료가 없으면 빈 목록."""
        try:
            records, _ = self.cache.load_day(previous)
        except Exception:
            return []
        ranked = sorted((r.trade_value or 0, code) for code, r in records.items())
        wanted = {code for _, code in ranked[-self.PRIORITY:]}
        return [item for item in items if item[0] in wanted]

    def partial_view(self, mode, market):
        """수집 중 임시 순위: 이미 받은 종목만으로 계산하고, 임시임을 분명히 표시한다."""
        markets = set(core.selected_markets(market))
        today = self.clock().date().isoformat()
        now = time.monotonic()
        with self.book_lock:
            if self.book_day != today:
                return None
            entries = [e for e in self.book.values() if e[0].market in markets]
        if not entries:
            return None
        entries.sort(key=lambda e: (-e[0].metric(mode), e[0].code))
        self.hot = tuple((e[0].code, e[0].name, e[0].market) for e in entries[:self.HOT])
        free = threading.Event()
        total = sum(len(self.masters.universe(m, free)) for m in markets)
        warnings = [f"임시 순위: 전일 상위 종목부터 먼저 조회 · 수집 {len(entries):,}/{total:,}종목 · 나머지는 몇 초 안에 채워집니다"]
        return core.Snapshot(mode, today, self.clock(), tuple(e[0] for e in entries[:100]), (),
                             tuple(warnings), complete=False, previous_date=self.book_prev)

    def book_view(self, mode, market, cancel, wait=0.0):
        """장부에서 TOP100을 즉시 계산한다. 신선도가 모자라면 wait초까지 순환 조회를 기다린다."""
        markets = core.selected_markets(market)
        items = []
        for selected in markets:
            items.extend(self.masters.universe(selected, cancel))
        codes = [code for code, _, _ in items]
        deadline = time.monotonic() + wait
        while True:
            view = self.try_view(mode, codes)
            if view is not None or time.monotonic() >= deadline:
                return view
            if cancel.wait(0.1):
                raise core.Cancelled()

    def try_view(self, mode, codes):
        today = self.clock().date().isoformat()
        now = time.monotonic()
        with self.book_lock:
            if self.book_day != today:
                return None
            entries = [e for e in map(self.book.get, codes) if e is not None]
        missing = len(codes) - len(entries)
        if not entries or missing > 0.02 * len(codes):
            return None
        entries.sort(key=lambda e: (-e[0].metric(mode), e[0].code))
        top = entries[:self.HOT]
        self.hot = tuple((e[0].code, e[0].name, e[0].market) for e in top)
        hot_age = max(now - e[1] for e in top)
        ages = [now - e[1] for e in entries]
        stale = sum(1 for age in ages if age > self.COLD_MAX_AGE)
        if hot_age > self.HOT_MAX_AGE or stale > 0.02 * len(codes):
            return None
        warnings = [f"실시간 순환 조회 · 상위 {self.HOT}종목 최대 {hot_age:.1f}초 · 전체 최대 {max(ages):.0f}초 전 수신"]
        if missing or stale:
            warnings.append(f"미수신 {missing}종목 · 지연 {stale}종목: 순위 일부 불완전")
        return core.Snapshot(mode, today, self.clock(), tuple(e[0] for e in entries[:100]), (),
                             tuple(warnings), complete=not (missing or stale), previous_date=self.book_prev)

    def fetch(self, mode, cancel, market="ALL"):
        if mode not in core.MODES:
            raise core.DataError("지원하지 않는 순위 기준입니다")
        if market == "US":
            return self.overseas.fetch(mode, cancel)
        if market == "US_SURGE":
            return self.overseas.fetch_surge(cancel)
        today = self.clock().date().isoformat()
        if self.book_day == today and self.book_prev:
            self.ensure_sweeper(today)
            view = self.book_view(mode, market, cancel, wait=0.0 if self.warming() else 3.0)
            if view is None and self.warming():
                view = self.partial_view(mode, market)
            if view is not None:
                return view
        selection = core.Selection(None, mode, market)
        data_date, previous = self.calendar.latest(cancel)
        if data_date < self.clock().date().isoformat() and not self.closed_batch_ok(data_date, cancel):
            result = core.HistoricalService(self.cache, self.history, self, self.clock).query(
                core.Selection(data_date, mode, market), cancel, progress_selection=selection)
            return result
        started = self.clock()
        items = []
        for selected in core.selected_markets(market):
            items.extend(self.masters.universe(selected, cancel))
        first = self.priority_items(items, previous) if data_date == self.clock().date().isoformat() else []
        if first:
            quotes, errors, stamps = self.scan_items(first, selection, cancel, report=False)
            core.check_cancel(cancel)
            if quotes:
                self.store(quotes, data_date, stamps)
                self.book_prev = previous
                self.ensure_sweeper(data_date)
                view = self.partial_view(mode, market)
                if view is not None:
                    return view
        quotes, errors, stamps = self.scan_items(items, selection, cancel)
        core.check_cancel(cancel)
        if not quotes:
            raise core.DataError("표시할 KIS 시세가 없습니다")
        after_date, after_previous = self.calendar.latest(cancel)
        if (data_date, previous) != (after_date, after_previous) or started.date() != self.clock().date():
            raise core.DataError("조회 중 거래일이 바뀌었습니다. 새 거래일로 다시 조회합니다", 5)
        if data_date == self.clock().date().isoformat():
            self.store(quotes, data_date, stamps)
            self.book_prev = previous
            self.ensure_sweeper(data_date)  # 이후 화면 갱신은 장부에서 즉시 계산한다
        quotes.sort(key=lambda q: (-q.metric(mode), q.code))
        warnings = [f"전체 종목 순차 조회 {started:%H:%M:%S}~{self.clock():%H:%M:%S} KST · 종목별 수신 시점 차이 있음"]
        if errors:
            warnings.append(f"응답 누락·오류 {errors}건: 순위 불완전")
        return core.Snapshot(mode, data_date, self.clock(), tuple(quotes[:100]), (), tuple(warnings),
                             complete=not errors, previous_date=previous)
