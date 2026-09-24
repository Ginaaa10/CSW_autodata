import httpx, re, logging
from urllib.parse import urljoin
logging.basicConfig(level=logging.INFO)

def _parse_login_form(html, base_url):
    match = re.search(r'<form[^>]*\saction="([^"]*)"', html, re.IGNORECASE)
    action = match.group(1) if match else "/login"
    login_post_url = urljoin(base_url + "/", action.lstrip("/"))
    return action, login_post_url

def _get_csrf_token(html):
    match = re.search(r'<input\s+type="hidden"\s+name="([^"]+)"\s+value="([^"]+)"', html)
    if match:
        name, value = match.group(1), match.group(2)
        if "csrf" in name.lower() or "token" in name.lower():
            return name, value
    match2 = re.search(r'<input\s+type="hidden"\s+name="([^"]*csrf[^"]*)"\s+value="([^"]+)"', html, re.IGNORECASE)
    if match2:
        return match2.group(1), match2.group(2)
    return None, None

def _is_error_page(text):
    error_kw = ["用户不存在", "用戶不存在", "密碼錯誤", "密码错误", "登录失败", "登錄失敗",
                 "invalid", "unauthorized", "forbidden"]
    text_lower = text.lower()
    for kw in error_kw:
        if kw in text_lower:
            return True
    return False

class WebScraper:
    def __init__(self):
        self.base_url = "http://10.144.38.15:9100"
        self.login_url = self.base_url + "/"
        self.data_url = self.base_url + "/DataAnalysis/NTF/"
        self.api_url = self.base_url + "/DataAnalysis/GetSummaryData/"
        self.client = httpx.Client(verify=False, follow_redirects=True, timeout=30)
        self.logged_in = False

    def login(self):
        resp = self.client.get(self.login_url)
        print(f"1. GET login page: {resp.status_code}")
        _, form_action_url = _parse_login_form(resp.text, self.base_url)
        csrf_name, csrf_val = _get_csrf_token(resp.text)
        print(f"2. Form action: {form_action_url}, CSRF: {csrf_val[:20] if csrf_val else 'None'}")
        base_headers = {"Content-Type": "application/x-www-form-urlencoded", "Referer": self.login_url}
        data = {"username": "gina_ruan", "password": "`12qazxcvbnm,./"}
        if csrf_name and csrf_val:
            data[csrf_name] = csrf_val
            r2 = self.client.post(form_action_url, data=data, headers=base_headers, follow_redirects=True)
            print(f"3. POST login: {r2.status_code}, len={len(r2.text)}")
            if _is_error_page(r2.text):
                print("ERROR: Error page detected")
            else:
                print("Login OK - no error page")
                self.client.post(urljoin(self.base_url, "/Report/NTF/"), data={"username": "gina_ruan"})
                self.client.get(self.data_url)
                self.logged_in = True
                print("4. Session established")

    def fetch(self):
        if not self.logged_in:
            return
        data_page = self.client.get(self.data_url)
        print(f"5. GET NTF data page: {data_page.status_code}")
        csrf_name, csrf_val = _get_csrf_token(data_page.text)
        print(f"6. NTF CSRF: {csrf_val[:20] if csrf_val else 'None'}")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Referer": self.data_url}
        if csrf_val:
            headers["X-CSRFToken"] = csrf_val
        resp = self.client.post(self.api_url, data={"line":"ALL","side":"ALL","station":"ALL","start_time":"2026-05-25 00:00:00","end_time":"2026-05-27 23:59:59"}, headers=headers)
        print(f"7. POST API: {resp.status_code}")
        if resp.status_code != 200:
            print(f"8. ERROR: {resp.text[:300]}")
        else:
            data = resp.json()
            print(f"8. OK: totalpcs={data.get('totalpcs','?')}")

s = WebScraper()
s.login()
s.fetch()
s.client.close()
