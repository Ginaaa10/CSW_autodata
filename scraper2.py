import re
import httpx
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SRC2_BASE = "http://10.144.38.11:5000"


class WebScraper2:
    def __init__(self):
        self.client = httpx.Client(verify=False, follow_redirects=True, timeout=30)
        self.logged_in = False

    def _get_csrf_token(self, html: str) -> str:
        match = re.search(
            r'<input\s+type="hidden"\s+name="csrf_token"\s+value="([^"]+)"', html
        )
        return match.group(1) if match else ""

    def login(self, username: str = None, password: str = None) -> bool:
        if not username or not password:
            raise ValueError("Username and password required")
        resp = self.client.get(f"{SRC2_BASE}/data_query")
        csrf = self._get_csrf_token(resp.text)
        login_resp = self.client.post(
            f"{SRC2_BASE}/login",
            data={"username": username, "password": password, "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if "登录" in login_resp.text and "用户名" in login_resp.text:
            raise Exception("Login failed - still on login page")
        self.logged_in = True
        logger.info("Source2 login successful")
        return True

    def fetch_data(self, start_date: str, end_date: str, line: str = "ALL") -> dict:
        if not self.logged_in:
            raise Exception("Not logged in")
        page = self.client.get(f"{SRC2_BASE}/data_query")
        csrf = self._get_csrf_token(page.text)
        resp = self.client.post(
            f"{SRC2_BASE}/data_query",
            data={
                "csrf_token": csrf,
                "start_date": start_date,
                "end_date": end_date,
                "line": line,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{SRC2_BASE}/data_query",
            },
        )
        return self._parse_table(resp.text, start_date, end_date)

    def _parse_table(self, html: str, start_date: str, end_date: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="result-table")
        if not table:
            table = soup.find("table", {"id": "result-table"}) or soup.find("table", class_=re.compile("table"))
        if not table:
            return {"rows": [], "columns": []}
        headers = []
        thead = table.find("thead")
        if thead:
            ths = thead.find_all("th")
            headers = [th.get_text(strip=True) for th in ths]
        rows_data = []
        tbody = table.find("tbody")
        if tbody:
            for tr in tbody.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    rows_data.append(cells)
        return {"columns": headers, "rows": rows_data}

    def close(self):
        self.client.close()
