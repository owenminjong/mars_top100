"""외국인·기관 수급 조회 (읽기 전용). 기존 시세·분봉 로직과 분리된 부가기능.

- 확정 일별 수급: 주식현재가 투자자 (FHKST01010900)
  개인·외국인·기관계의 매수/매도/순매수 수량·대금. KIS 안내상 당일 자료는 장 종료 후 제공된다.
- 장중 가집계: 종목별 외인기관 추정가집계 (HHPTJ04160200)
  증권사 직원이 장중에 입력한 순매수 수량(주)의 단순 누계다. 입력 시각은 외국인 09:30·11:20·13:20·14:30,
  기관 10:00·11:20·13:20·14:30이며 체결 단위의 실시간 자료가 아니다. 대금은 제공되지 않는다.

받은 자료는 파일·DB로 저장하지 않는다. 순매수는 음수(순매도)가 정상 값이므로 required_int를 쓰지 않는다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import mars_core as core
from kis_provider import KISCalendar, kis_date, response_rows

INVESTOR = "/uapi/domestic-stock/v1/quotations/inquire-investor"
ESTIMATE = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
MAX_ROWS = 100
ESTIMATE_OPEN = 9 * 60 + 30
SLOTS = {"1": "09:30", "2": "10:00", "3": "11:20", "4": "13:20", "5": "14:30"}
GROUPS = (("foreign", "frgn"), ("institution", "orgn"), ("person", "prsn"))
FIELDS = (("buy_qty", "shnu_vol"), ("sell_qty", "seln_vol"), ("net_qty", "ntby_qty"),
          ("buy_amt", "shnu_tr_pbmn"), ("sell_amt", "seln_tr_pbmn"), ("net_amt", "ntby_tr_pbmn"))


def signed(row, key):
    """음수(순매도)를 허용하는 정수 읽기. 빈 값·비정상 값은 0이 아니라 None."""
    value = core.number(row.get(key))
    return None if value is None else int(value.to_integral_value())


@dataclass(frozen=True)
class Side:
    """한 투자자군의 하루 수급. 금액은 원 단위 정수, 단위를 확인하지 못하면 None."""
    buy_qty: int | None = None
    sell_qty: int | None = None
    net_qty: int | None = None
    buy_amount: int | None = None
    sell_amount: int | None = None
    net_amount: int | None = None


@dataclass(frozen=True)
class FlowDay:
    day: str
    close: int | None
    foreign: Side
    institution: Side
    person: Side

    @property
    def confirmed(self):
        return self.foreign.net_qty is not None and self.institution.net_qty is not None

    @property
    def combined_amount(self):
        a, b = self.foreign.net_amount, self.institution.net_amount
        return None if a is None or b is None else a + b


@dataclass(frozen=True)
class Estimate:
    slot: str
    label: str
    foreign: int | None
    institution: int | None
    total: int | None


def raw_row(row):
    raw = {"close": core.first_number(row, ("stck_clpr",), nonnegative=True, integral=True)}
    for _, prefix in GROUPS:
        for name, suffix in FIELDS:
            raw[f"{prefix}_{name}"] = signed(row, f"{prefix}_{suffix}")
    return raw


def detect_scale(raws):
    """대금 필드의 단위를 같은 응답의 (순매수 대금 ÷ 순매수 수량 × 종가)로 확인한다.

    KIS 투자자 대금은 백만원 단위로 알려져 있으나 추정하지 않고 응답으로 교차 확인한다.
    비율이 1e-6 근처면 백만원, 1 근처면 원이다. 표본이 부족하면 백만원으로 보고 '추정'을 표시한다.
    어느 쪽도 아니면 금액을 숨긴다(수량은 그대로 표시).
    """
    ratios = []
    for raw in raws:
        price = raw.get("close")
        if not price:
            continue
        for _, prefix in GROUPS:
            qty, amount = raw.get(f"{prefix}_net_qty"), raw.get(f"{prefix}_net_amt")
            if qty is None or amount is None or abs(qty) < 1000 or amount == 0:
                continue
            ratios.append(abs(amount) / (abs(qty) * price))
    if len(ratios) < 3:
        return 1_000_000, "대금 단위는 백만원으로 추정(표본 부족)"
    ratios.sort()
    median = ratios[len(ratios) // 2]
    if 1e-7 <= median <= 1e-5:
        return 1_000_000, ""
    if 0.1 <= median <= 10:
        return 1, ""
    return None, "대금 단위를 확인하지 못해 금액을 표시하지 않습니다(수량만 표시)"


def build_day(day, raw, scale):
    def money(value):
        return None if value is None or scale is None else value * scale
    sides = [Side(raw[f"{p}_buy_qty"], raw[f"{p}_sell_qty"], raw[f"{p}_net_qty"],
                  money(raw[f"{p}_buy_amt"]), money(raw[f"{p}_sell_amt"]), money(raw[f"{p}_net_amt"]))
             for _, p in GROUPS]
    return FlowDay(day, raw["close"], *sides)


def parse_confirmed(payload, today):
    rows = response_rows(payload, "output")
    if len(rows) > MAX_ROWS:
        raise core.DataError("KIS 수급 응답 건수가 제한을 초과했습니다")
    parsed, seen = [], set()
    for row in rows:
        if all(v in (None, "") for v in row.values()):
            continue
        day = kis_date(row.get("stck_bsop_date"))
        if day > today or day in seen:
            raise core.DataError("KIS 수급 거래일 범위가 잘못되었습니다")
        seen.add(day)
        parsed.append((day, raw_row(row)))
    scale, note = detect_scale([raw for _, raw in parsed])
    days = sorted((build_day(day, raw, scale) for day, raw in parsed), key=lambda d: d.day, reverse=True)
    return tuple(days), note


def parse_estimate(payload):
    key = "output2" if isinstance(payload.get("output2"), list) else "output"
    rows = response_rows(payload, key)
    if len(rows) > 20:
        raise core.DataError("KIS 가집계 응답 건수가 제한을 초과했습니다")
    items = []
    for row in rows:
        if all(v in (None, "") for v in row.values()):
            continue
        slot = str(row.get("bsop_hour_gb", "")).strip()
        items.append(Estimate(slot, SLOTS.get(slot, slot or "?"), signed(row, "frgn_fake_ntby_qty"),
                              signed(row, "orgn_fake_ntby_qty"), signed(row, "sum_fake_ntby_qty")))
    items.sort(key=lambda e: (not e.slot.isdigit(), int(e.slot) if e.slot.isdigit() else 0, e.slot))
    return tuple(items)


def latest_estimate(estimates, field):
    """가장 최근에 입력된 값. 빈 칸과 0은 미입력일 수 있어 건너뛴다."""
    for item in reversed(estimates):
        value = getattr(item, field)
        if value:
            return item, value
    return None


@dataclass(frozen=True)
class FlowRequest:
    serial: int
    code: str
    name: str


@dataclass(frozen=True)
class FlowResult:
    code: str
    name: str
    session_day: str
    previous_day: str
    live: bool
    days: tuple
    estimates: tuple
    notes: tuple
    received_at: dt.datetime
    retry_after: int = 0

    @property
    def today_final(self):
        return any(d.day == self.session_day and d.confirmed for d in self.days)


class FlowService:
    """한 종목의 확정 일별 수급 + (장중이면) 가집계. KISClient의 공용 호출 제한을 그대로 따른다."""
    def __init__(self, client, calendar=None, clock=core.now_kst):
        self.client, self.clock = client, clock
        self.calendar = calendar or KISCalendar(client, clock)

    def query(self, request, cancel):
        code = core.stock_code({"code": request.code})
        now = self.clock()
        today = now.date().isoformat()
        session_day, previous = self.calendar.latest(cancel)
        payload = self.client.get(INVESTOR, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, cancel)
        days, note = parse_confirmed(payload, today)
        core.check_cancel(cancel)
        live = session_day == today
        estimates, notes, retry = (), [note] if note else [], 0
        # 오늘 거래일의 첫 입력 시각(09:30) 전에는 가집계를 요청하지 않는다: 응답에 날짜가 없어 전날 값과 구분할 수 없다.
        if live and now.hour * 60 + now.minute >= ESTIMATE_OPEN:
            try:
                estimates = parse_estimate(self.client.get(ESTIMATE, {"MKSC_SHRN_ISCD": code}, cancel))
            except core.DataError as exc:
                retry = getattr(exc, "retry_after", 0)
                notes.append(f"장중 가집계 조회 실패: {exc}")
        core.check_cancel(cancel)
        return FlowResult(code, request.name, session_day, previous, live, days, estimates,
                          tuple(notes), now, retry)
