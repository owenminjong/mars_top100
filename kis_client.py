"""KIS read-only REST transport, shared throttling and encrypted token lifecycle."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from mars_core import Cancelled, DataError, KST, LOG, check_cancel
from kis_storage import SecureStore, validate_password

BASE_URL = "https://openapi.koreainvestment.com:9443"
ALLOWED_APIS = {
    "/uapi/domestic-stock/v1/quotations/intstock-multprice": "FHKST11300006",
    "/uapi/domestic-stock/v1/quotations/inquire-price": "FHKST01010100",
    "/uapi/domestic-stock/v1/quotations/inquire-index-price": "FHPUP02100000",
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice": "FHKST03010100",
    "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice": "FHKUP03500100",
    "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": "FHKST03010230",
    "/uapi/domestic-stock/v1/quotations/inquire-investor": "FHKST01010900",
    "/uapi/domestic-stock/v1/quotations/investor-trend-estimate": "HHPTJ04160200",
    "/uapi/overseas-stock/v1/ranking/trade-pbmn": "HHDFS76320010",
    "/uapi/overseas-stock/v1/ranking/trade-vol": "HHDFS76310010",
    "/uapi/overseas-stock/v1/ranking/updown-rate": "HHDFS76290000",
    "/uapi/overseas-price/v1/quotations/dailyprice": "HHDFS76240000",
    "/uapi/overseas-price/v1/quotations/inquire-time-indexchartprice": "FHKST03030200",
}
TOKEN_ERRORS = {"EGW00121", "EGW00123"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials must never be forwarded by a redirect, including same-host redirects.
        raise DataError("KIS 서버가 예기치 않은 리디렉션을 반환했습니다")


def validate_keys(key, secret):
    values = (key.strip(), secret.strip())
    if any(not 10 <= len(v) <= 512 or any(not 33 <= ord(c) <= 126 for c in v) for v in values):
        raise DataError("App Key와 App Secret을 공백 없이 정확히 입력하세요")
    return values


def server_detail(data, secrets=()):
    """Sanitized server error code/message for display. Never echoes credentials."""
    if not isinstance(data, dict):
        return ""
    code = next((str(data[k]) for k in ("msg_cd", "error_code") if data.get(k)), "")
    code = code if re.fullmatch(r"[A-Za-z0-9_]{1,16}", code) else ""
    text = next((data[k] for k in ("msg1", "error_description", "message") if isinstance(data.get(k), str)), "")
    text = " ".join("".join(c for c in text if c.isprintable()).split())[:100]
    if any(v and len(v) >= 8 and v in text for v in secrets):
        text = ""
    return " · ".join(x for x in (code, text) if x)


class HttpTransport:
    def __init__(self, opener=None):
        self.local = threading.local()
        self.opener = opener

    def request(self, path, headers, cancel, params=None, body=None):
        check_cancel(cancel)
        if path not in ("/oauth2/tokenP", "/oauth2/revokeP") and path not in ALLOWED_APIS:
            raise DataError("지원하지 않는 API 요청입니다")
        if path in ALLOWED_APIS and body is not None:
            raise DataError("시세 조회에는 GET 요청만 허용됩니다")
        if path in ("/oauth2/tokenP", "/oauth2/revokeP") and (not isinstance(body, dict) or params):
            raise DataError("인증 요청 형식이 올바르지 않습니다")
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method="POST" if body is not None else "GET")
        opener = self.opener
        if opener is None:
            if not hasattr(self.local, "opener"):
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                self.local.opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
            opener = self.local.opener
        response = None
        try:
            try:
                response = opener.open(request, timeout=8)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.code
                raw_retry = response.headers.get("Retry-After", "5")
                retry = min(300, max(5, int(raw_retry))) if str(raw_retry).isdigit() else 5
                chunks, size = [], 0
                deadline = time.monotonic() + 20
                read = getattr(response, "read1", response.read)
                while True:
                    check_cancel(cancel)
                    if time.monotonic() > deadline:
                        raise DataError("KIS 응답 수신 시간이 초과되었습니다", 5)
                    chunk = read(min(65536, 2 * 1024 * 1024 + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise DataError("KIS 응답 크기가 제한을 초과했습니다")
                    chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                payload = None
            check_cancel(cancel)
            return status, payload, retry
        except (urllib.error.URLError, TimeoutError, OSError):
            # Never expose request headers, token bodies, proxy URLs or raw response messages.
            raise DataError("KIS 연결 실패: 인터넷 연결 또는 서버 점검 여부를 확인하세요", 5) from None


class KISClient:
    def __init__(self, store=None, transport=None, state=None, interval=0.06, clock=time.time):
        self.store = store or SecureStore()
        self.transport = transport or HttpTransport()
        self.state = dict(self.store.load() if state is None else state)
        self.interval = interval  # Adaptive: slows after a rate-limit response, recovers after successes.
        self.floor = interval
        self.good = 0
        self.last_slow = 0.0
        self.adapt_lock = threading.Lock()
        self.clock = clock
        self.auth_lock = threading.RLock()
        self.rate_lock = threading.Lock()
        # A start-time limiter alone permits several concurrent requests to remain
        # in flight. KIS can count those as a burst even when their start times are
        # spaced, so serialize the complete REST exchange as well.
        self.request_lock = threading.Lock()
        self.next_call = 0.0
        self.not_before = 0.0
        self.spare = None  # In-memory only: a fresh token whose quote check failed, reused on retry.

    @property
    def configured(self):
        try:
            validate_keys(self.state.get("app_key", ""), self.state.get("app_secret", ""))
            return True
        except (DataError, AttributeError):
            return False

    def configure(self, key, secret, cancel, password=None):
        """Validate a candidate without replacing working credentials on failure."""
        key, secret = validate_keys(key, secret)
        if password is not None:
            validate_password(password)
        owner = self

        class CandidateStore:
            def save(self, payload):
                # Persist only the cooldown until both authentication and quote access succeed.
                owner.state["last_token_attempt"] = payload.get("last_token_attempt", 0)
                owner.store.record_attempt(owner.state["last_token_attempt"])

        same = key == self.state.get("app_key") and secret == self.state.get("app_secret")
        last_attempt = max(self.state.get("last_token_attempt", 0), self.store.last_attempt())
        spare = self.spare if self.spare and self.spare.get("app_key") == key and self.spare.get("app_secret") == secret else None
        state = dict(self.state) if same else (dict(spare) if spare else {
            "app_key": key, "app_secret": secret,
            "last_token_attempt": last_attempt})
        state["last_token_attempt"] = max(last_attempt, state.get("last_token_attempt", 0)) if spare and not same else last_attempt
        candidate = KISClient(CandidateStore(), self.transport, state=state,
                              interval=self.interval, clock=self.clock)
        # A token alone does not prove that this key can read the production quote endpoints.
        try:
            candidate.get("/uapi/domestic-stock/v1/quotations/intstock-multprice", {
                "FID_COND_MRKT_DIV_CODE_1": "J", "FID_INPUT_ISCD_1": "005930"}, cancel)
        except Exception:
            # Keep a good token in memory so a retry does not burn another 1-per-minute issuance.
            if isinstance(candidate.state.get("access_token"), str):
                self.spare = dict(candidate.state)
            raise
        check_cancel(cancel)
        self.store.install(candidate.state, password=password)
        self.spare = None
        self.state = candidate.state
        self.next_call = candidate.next_call
        self.not_before = 0

    def clear(self):
        self.spare = None
        try:
            self.store.delete()
        finally:
            self.state.clear()

    def lock(self):
        """Call only after workers drain. Python strings cannot be reliably zeroized."""
        self.spare = None
        self.state.clear()
        self.store.lock()

    def unlock(self, password):
        state = self.store.unlock(password)
        state["last_token_attempt"] = max(state.get("last_token_attempt", 0), self.store.last_attempt())
        self.state = state

    def migrate_legacy(self, password=None):
        if password is not None:
            validate_password(password)
        state = self.store.read_legacy()
        state["last_token_attempt"] = max(state.get("last_token_attempt", 0), self.store.last_attempt())
        self.store.record_attempt(state["last_token_attempt"])
        self.store.install(state, password=password)
        self.state = state

    def revoke_and_clear(self, cancel):
        """Revoke only the known token, never issue one to revoke it. Always delete locally."""
        revoked = False
        try:
            if self.configured and self.state.get("access_token"):
                status, data, _ = self.transport.request("/oauth2/revokeP", {
                    "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"
                }, cancel, body={"appkey": self.state["app_key"], "appsecret": self.state["app_secret"],
                                 "token": self.state["access_token"]})
                revoked = status == 200 and isinstance(data, dict) and str(data.get("code")) == "200"
        except Exception:
            # Never expose remote bodies/exceptions. A failure must not prevent local removal.
            revoked = False
        finally:
            self.clear()
        return revoked

    def throttle(self, cancel):
        while not self.rate_lock.acquire(timeout=0.1):
            check_cancel(cancel)
        try:
            while True:
                now = time.monotonic()
                remaining = self.not_before - now
                if remaining > 20:
                    raise DataError(f"KIS 호출 제한: {math.ceil(remaining)}초 후 재시도합니다", math.ceil(remaining))
                wait = max(self.next_call, self.not_before) - now
                if wait <= 0:
                    break
                if cancel.wait(wait):
                    raise Cancelled()
            self.next_call = time.monotonic() + self.interval
        finally:
            self.rate_lock.release()

    def _slow_down(self, backoff, sent=0.0):
        # Several in-flight requests can be rejected by the same burst: slow down once per burst.
        with self.adapt_lock:
            if sent >= self.last_slow:
                self.interval = min(0.6, max(self.interval * 1.3, 0.12))
                self.last_slow = time.monotonic()
                self.good = 0
                LOG.warning("KIS 호출 한도 감지: 요청 간격 %.2f초로 조정", self.interval)
            self.not_before = max(self.not_before, time.monotonic() + backoff)

    def _speed_up(self):
        self.good += 1
        if self.good >= 25 and self.interval > self.floor:
            self.interval = max(self.floor, self.interval * 0.9)
            self.good = 0

    def token(self, cancel, rejected=None):
        while not self.auth_lock.acquire(timeout=0.1):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            if not self.configured:
                raise DataError("KIS 설정에서 실전투자 App Key와 App Secret을 입력하세요")
            now = self.clock()
            token = self.state.get("access_token")
            expires = self.state.get("expires_at", 0)
            if rejected and token == rejected:
                self.state.pop("access_token", None)
                self.state["expires_at"] = 0
                self.store.save(self.state)
                token = None
            if isinstance(token, str) and isinstance(expires, (int, float)) and expires > now + 120:
                return token
            last = self.state.get("last_token_attempt", 0)
            if not isinstance(last, (int, float)) or last > now + 5:
                last = 0  # A timestamp from the future (clock change/corrupt file) must not block forever.
            delay = min(61, math.ceil(61 - (now - last)))
            if delay > 0:
                raise DataError(f"토큰 발급 대기: {delay}초 후 다시 조회하세요", delay)
            self.state["last_token_attempt"] = now
            self.store.save(self.state)  # Persist cooldown even if the request fails or app restarts.
            with self.request_lock:
                self.throttle(cancel)
                status, data, retry = self.transport.request("/oauth2/tokenP", {
                    "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"
                }, cancel, body={"grant_type": "client_credentials", "appkey": self.state["app_key"],
                                "appsecret": self.state["app_secret"]})
            if status != 200 or not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
                detail = server_detail(data, (self.state["app_key"], self.state["app_secret"]))
                LOG.warning("토큰 발급 실패 (HTTP %s, %s)", status, detail.split(" · ")[0] if detail else "-")
                if "EGW00133" in detail:
                    raise DataError("KIS 토큰 발급은 1분에 1번만 가능합니다. 61초 뒤 다시 시도하세요 (" + detail + ")", 61)
                raise DataError(f"KIS 인증 실패 (HTTP {status}" + (f", {detail}" if detail else "") +
                                "): 실전투자용 키·Secret, Open API 서비스 신청 상태를 확인하세요", max(61, retry))
            token = data["access_token"]
            if not token or len(token) > 16384 or any(not 33 <= ord(c) <= 126 for c in token):
                raise DataError("KIS 토큰 응답 형식이 잘못되었습니다", 61)
            expiry = None
            try:
                lifetime = int(data["expires_in"])
                if 120 < lifetime <= 172800:
                    expiry = now + lifetime
            except (KeyError, ValueError, TypeError):
                pass
            try:
                if data.get("access_token_token_expired"):
                    absolute = dt.datetime.strptime(data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()
                    expiry = absolute if expiry is None else min(expiry, absolute)
            except (ValueError, TypeError):
                pass  # Optional field: a format quirk must not reject a valid token.
            if expiry is None or expiry <= now + 120:
                LOG.warning("토큰 만료 정보 이상")
                raise DataError("KIS 토큰 만료일을 확인할 수 없습니다. PC의 날짜·시간을 확인하세요", 61)
            self.state.update(access_token=token, expires_at=expiry)
            self.store.save(self.state)
            return token
        finally:
            self.auth_lock.release()

    def get(self, path, params, cancel):
        if path not in ALLOWED_APIS:
            raise DataError("지원하지 않는 시세 API입니다")
        token = self.token(cancel)
        auth_retry = server_retry = rate_retry = 0
        while True:
            # Start times are spaced by throttle(); the exchanges themselves may overlap so that
            # server latency no longer caps throughput. Rate-limit hits slow everyone down.
            self.throttle(cancel)
            sent = time.monotonic()
            status, data, retry = self.transport.request(path, {
                "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                "authorization": "Bearer " + token, "appkey": self.state["app_key"],
                "appsecret": self.state["app_secret"], "tr_id": ALLOWED_APIS[path], "custtype": "P"
            }, cancel, params=params)
            code = str(data.get("msg_cd", "")) if isinstance(data, dict) else ""
            rate_limited = status == 429 or code == "EGW00201"
            if rate_limited:
                rate_retry += 1
                self._slow_down(min(2.0, 0.3 * rate_retry), sent)
            if rate_limited:
                if rate_retry > 8:
                    raise DataError("KIS 초당 호출 한도를 계속 초과했습니다. 잠시 후 자동 재시도합니다", 10)
                continue  # Same request again, no scan restart.
            if status == 401 or code in TOKEN_ERRORS:
                if auth_retry == 0:
                    auth_retry = 1
                    token = self.token(cancel, rejected=token)
                    continue
                raise DataError("KIS 토큰이 거부되었습니다. KIS 설정에서 키를 확인하세요", 61)
            if status in (500, 502, 503, 504) and server_retry < 2:
                if cancel.wait(2 ** server_retry):
                    raise Cancelled()
                server_retry += 1
                continue
            if status != 200 or not isinstance(data, dict) or str(data.get("rt_cd")) != "0":
                safe_code = code if re.fullmatch(r"[A-Z0-9]{1,16}", code) else "응답 오류"
                detail = server_detail(data, (self.state.get("app_key", ""), self.state.get("app_secret", ""), token))
                LOG.warning("시세 조회 실패 (HTTP %s, %s)", status, safe_code)
                raise DataError(f"KIS 조회 실패 (HTTP {status}, {detail or safe_code}). 서비스 권한·점검 여부를 확인하세요", 5)
            self._speed_up()
            return data

