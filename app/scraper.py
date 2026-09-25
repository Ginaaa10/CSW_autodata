import re
import json
import httpx
import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from app.config import settings

logger = logging.getLogger(__name__)

DETAIL_FIELD_KEYS = [
    "SN", "ai_time", "pic_date_time", "ai_predict",
    "line", "side", "station", "Report_Error", "Report_User",
    "field9", "pic_url",
]


def _extract_first_number(text: str) -> str:
    if not text or text == "0 pcs 0.0%":
        return "0"
    match = re.search(r"(\d+)", str(text))
    return match.group(1) if match else "0"


def detect_source_type(base_url: str) -> str:
    url_lower = base_url.lower().strip("/")
    if url_lower == "feeder" or url_lower.startswith("feeder:"):
        return "feeder"
    if ":5000" in url_lower or "data_query" in url_lower:
        return "data_query"
    return "get_summary"


class WebScraper:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or settings.WEB_BASE_URL).rstrip("/")
        self.source_type = detect_source_type(self.base_url)

        if self.source_type == "data_query":
            base = self.base_url.rstrip("/")
            if not base.endswith("data_query"):
                self.data_query_url = base + "/data_query"
            else:
                self.data_query_url = base
            self.login_page_url = self.data_query_url
            self.login_post_url = base.rstrip("/data_query").rstrip("/") + "/login"
            self.csrf_field = "csrf_token"
            # also set standard names for compat
            self.login_url = self.login_page_url
            self.data_url = self.data_query_url
        else:
            self.login_url = settings.WEB_LOGIN_URL
            self.data_url = settings.WEB_DATA_URL
            self.api_url = settings.WEB_GET_SUMMARY_API
            if base_url and self.base_url != settings.WEB_BASE_URL.rstrip("/"):
                self.login_url = self.base_url + "/"
                self.data_url = urljoin(self.base_url + "/", "DataAnalysis/NTF/")
                self.api_url = urljoin(self.base_url + "/", "DataAnalysis/GetSummaryData/")

        self._client = None
        self.logged_in = False

    @property
    def client(self):
        if self._client is None:
            self._client = httpx.Client(verify=False, follow_redirects=True, timeout=30)
        return self._client

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self.logged_in = False

    def _parse_login_form(self, html: str):
        match = re.search(r'<form[^>]*\saction="([^"]*)"', html, re.IGNORECASE)
        action = match.group(1) if match else "/login"
        login_post_url = urljoin(self.base_url + "/", action.lstrip("/"))
        return action, login_post_url

    def _get_csrf_token(self, html: str) -> tuple:
        match = re.search(
            r'<input\s+type="hidden"\s+name="([^"]+)"\s+value="([^"]+)"', html
        )
        if match:
            name, value = match.group(1), match.group(2)
            if "csrf" in name.lower() or "token" in name.lower():
                return name, value
        match2 = re.search(
            r'<input\s+type="hidden"\s+name="([^"]*csrf[^"]*)"\s+value="([^"]+)"',
            html, re.IGNORECASE
        )
        if match2:
            return match2.group(1), match2.group(2)
        return None, None

    def _is_error_page(self, text: str) -> bool:
        error_kw = ["用户不存在", "用戶不存在", "密碼錯誤", "密码错误", "登录失败", "登錄失敗",
                     "invalid", "unauthorized", "forbidden"]
        text_lower = text.lower()
        for kw in error_kw:
            if kw in text_lower:
                return True
        return False

    def _login_attempt(self, login_url: str, data: dict, headers: dict) -> bool:
        resp = self.client.post(login_url, data=data, headers=headers, follow_redirects=True)
        if resp.status_code in (301, 302, 303, 307, 308):
            resp = self.client.get(resp.headers.get("Location", self.data_url))
        if self._is_error_page(resp.text):
            raise Exception("server returned error page")
        if resp.status_code == 200 and '"ErrorMessage"' in resp.text:
            try:
                err = resp.json().get("ErrorMessage", "")
                if err:
                    raise Exception(err)
            except Exception:
                pass
        return True

    def login(self, username: str = None, password: str = None) -> bool:
        if self.source_type == "feeder":
            self.logged_in = True
            return True
        if self.source_type == "data_query":
            return self._login_data_query(username, password)
        return self._login_get_summary(username, password)

    # ---- login for data_query type (38.11:5000) ----
    def _login_data_query(self, username: str = None, password: str = None) -> bool:
        username = username or settings.WEB_USERNAME
        password = password or settings.WEB_PASSWORD
        if not username or not password:
            raise ValueError("Username and password are required")
        logger.info(f"Logging in (data_query) as: {username}")
        resp = self.client.get(self.login_page_url)
        csrf = self._get_csrf_token(resp.text)
        csrf_val = csrf[1] if csrf and csrf[1] else ""
        login_resp = self.client.post(
            self.login_post_url,
            data={"username": username, "password": password, "csrf_token": csrf_val},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if "登录" in login_resp.text and "用户名" in login_resp.text:
            raise Exception("Login failed - still on login page")
        self.logged_in = True
        logger.info("data_query login successful")
        return True

    # ---- login for get_summary type (38.15 Django) ----
    def _login_get_summary(self, username: str = None, password: str = None) -> bool:
        username = username or settings.WEB_USERNAME
        password = password or settings.WEB_PASSWORD
        if not username or not password:
            raise ValueError("Username and password are required")
        logger.info(f"Logging in as: {username}")

        resp = self.client.get(self.login_url)
        _, form_action_url = self._parse_login_form(resp.text)
        csrf_name, csrf_val = self._get_csrf_token(resp.text)

        base_headers = {"Content-Type": "application/x-www-form-urlencoded", "Referer": self.login_url}

        data = {"username": username, "password": password}

        if csrf_name and csrf_val:
            data[csrf_name] = csrf_val
            try:
                self._login_attempt(form_action_url, data, base_headers)
                self.client.post(urljoin(self.base_url, "/Report/NTF/"), data={"username": username})
                self.client.get(self.data_url)
                self.logged_in = True
                return True
            except Exception:
                pass

        data2 = {"username": username, "password": password}
        fallback_url = urljoin(self.base_url + "/", "verifylogin")
        try:
            self._login_attempt(fallback_url, data2, base_headers)
        except Exception as e:
            raise Exception(f"Login failed: {e}")

        self.client.post(urljoin(self.base_url, "/Report/NTF/"), data={"username": username})
        self.client.get(self.data_url)
        self.logged_in = True
        return True

    # ---- fetch for data_query type (38.11:5000) ----
    @staticmethod
    def _data_query_params_for_code(code: str) -> dict:
        code_key = (code or "STA5").strip().lower()
        if code_key in ("sta5", "sat5", "aa8"):
            return {"model": "all", "data_scope": "aa8"}
        if code_key == "fa":
            return {"model": "all", "data_scope": "fa"}
        return {"model": code_key.upper()}

    def _fetch_data_query(self, start_date: str, end_date: str, code: str = "sta5") -> list:
        start_date = (start_date or "")[:10]
        end_date = (end_date or "")[:10]
        code_params = self._data_query_params_for_code(code)
        model = code_params.get("model", "all")
        data_scope = code_params.get("data_scope", "")
        api_url = urljoin(self.base_url.rstrip("/data_query").rstrip("/") + "/", "api/data_query")
        logger.info(f"data_query URL: {api_url}, code={code}, model={model}, data_scope={data_scope}, dates={start_date}~{end_date}")
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "model": model,
            "line": "all",
            "station": "all",
            "side": "all",
            "num": "all",
            "interval": "day",
        }
        if data_scope:
            params["data_scope"] = data_scope
        resp = self.client.get(
            api_url,
            params=params,
            headers={"Referer": self.data_query_url},
        )
        logger.info(f"data_query GET {api_url} ({code}/{data_scope or model}) -> {resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200:
            logger.warning(f"data_query non-200: {resp.text[:200]}")
            return []
        try:
            data = resp.json()
            data = data if isinstance(data, list) else []
            if data:
                logger.info(f"data_query JSON keys: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
                logger.info(f"data_query first row: {data[0]}")
            return self._fill_missing_data_query_days(data, start_date, end_date)
        except json.JSONDecodeError:
            logger.info(f"data_query not JSON, parsing HTML table")
            parsed = self._parse_html_table(resp.text)
            if parsed:
                logger.info(f"data_query HTML parsed keys: {list(parsed[0].keys()) if isinstance(parsed[0], dict) else type(parsed[0])}")
                logger.info(f"data_query HTML first row: {parsed[0]}")
            return self._fill_missing_data_query_days(parsed, start_date, end_date)

    @staticmethod
    def _data_query_date_range(start_date: str, end_date: str) -> list[str]:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return [d for d in [start_date, end_date] if d]
        if end < start:
            start, end = end, start
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return dates

    @staticmethod
    def _zero_data_query_row(period: str) -> dict:
        return {
            "period": period,
            "ai_called_pass": 0,
            "ai_called_fail": 0,
            "ipqc_rechecked": 0,
            "recheck_rate": 0,
            "same_as_ai": 0,
            "ai_misjudged": 0,
            "ntf_rate": 0,
            "fail_rate": 0,
        }

    def _fill_missing_data_query_days(self, rows: list, start_date: str, end_date: str) -> list:
        wanted_dates = self._data_query_date_range(start_date, end_date)
        normalized: dict[str, dict] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            period = str(row.get("period") or row.get("date") or row.get("Date") or "").strip()
            if not period:
                continue
            normalized[period] = row
        filled = []
        for day in wanted_dates:
            row = normalized.get(day)
            if row is None:
                row = self._zero_data_query_row(day)
            else:
                row = {**self._zero_data_query_row(day), **row, "period": day}
            filled.append(row)
        return filled

    def _parse_html_table(self, html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="result-table") or soup.find("table", class_=re.compile("table"))
        if not table:
            table = soup.find("table")
        if not table:
            return []
        headers = []
        thead = table.find("thead")
        if thead:
            headers = [th.get_text(strip=True) for th in thead.find_all("th")]
        if not headers:
            first_header_row = table.find("tr")
            if first_header_row and first_header_row.find_all("th"):
                headers = [th.get_text(strip=True) for th in first_header_row.find_all("th")]
        rows = []
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            row_headers = headers[:]
            if len(cells) > len(row_headers):
                row_headers.extend([f"field{i + 1}" for i in range(len(row_headers), len(cells))])
            rows.append(dict(zip(row_headers, cells)) if row_headers else cells)
        return rows

    # ---- fetch for get_summary type (38.15 Django) ----
    def fetch_summary_data(self, start_time=None, end_time=None, line="ALL", side="ALL", station="ALL") -> dict:
        if not self.logged_in:
            raise Exception("Not logged in. Call login() first.")
        if self.source_type == "data_query":
            rows = self._fetch_data_query(start_time or "", end_time or "", code=line or "sta5")
            return {"summary": {}, "details": rows}

        data_page = self.client.get(self.data_url)
        csrf_name, csrf_val = self._get_csrf_token(data_page.text)
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Referer": self.data_url}
        if csrf_val:
            headers["X-CSRFToken"] = csrf_val

        resp = self.client.post(
            self.api_url,
            data={"line": line, "side": side, "station": station,
                   "start_time": start_time, "end_time": end_time},
            headers=headers,
        )

        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}: {resp.text[:300]}")

        raw = resp.json()
        if isinstance(raw, list):
            raw = raw[0] if raw else {}

        ntf_raw = raw.get("ntfrate", "0 pcs 0.0%")
        leak_raw = raw.get("leakagerate", "0 pcs 0.0%")

        allsndata = raw.get("allsndata", [])
        details = []
        for item in allsndata:
            if isinstance(item, list):
                details.append(dict(zip(DETAIL_FIELD_KEYS, item)))
            elif isinstance(item, dict):
                details.append(item)
            else:
                details.append({"SN": str(item)})

        return {
            "summary": {
                "totalpcs": raw.get("totalpcs", 0),
                "failpcs": raw.get("failpcs", 0),
                "passrate": raw.get("passrate", "0%"),
                "ntfrate": _extract_first_number(ntf_raw),
                "leakagerate": _extract_first_number(leak_raw),
            },
            "details": details,
        }

    def fetch_by_code(self, start_date: str, end_date: str, code: str) -> list:
        if not self.logged_in:
            raise Exception("Not logged in.")
        if self.source_type == "get_summary":
            raise Exception("fetch_by_code only supported for data_query source type")
        return self._fetch_data_query(start_date, end_date, code)
