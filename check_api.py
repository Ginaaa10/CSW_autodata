import httpx, json
import os
from bs4 import BeautifulSoup

client = httpx.Client(timeout=30, verify=False)
r = client.get('http://10.144.38.11:5000/data_query')
soup = BeautifulSoup(r.text, 'html.parser')
csrf = soup.find('input', {'name': 'csrf_token'})
csrf_val = csrf['value'] if csrf else ''
r2 = client.post(
    'http://10.144.38.11:5000/login',
    data={
        'csrf_token': csrf_val,
        'username': os.environ["API_USERNAME"],
        'password': os.environ["API_PASSWORD"]
    },
    follow_redirects=True
)
print('Login:', r2.status_code)

# Fetch sta5 data
r3 = client.get('http://10.144.38.11:5000/api/data_query', params={
    'start_date': '2026-06-01', 'end_date': '2026-06-07',
    'model': 'STA5', 'line': 'all', 'station': 'all',
    'side': 'all', 'num': 'all', 'interval': 'day'
}, headers={'Referer': 'http://10.144.38.11:5000/data_query'})
j = r3.json()
print('\n=== API response fields ===')
if j:
    print('Keys:', list(j[0].keys()))
    for row in j:
        print(json.dumps(row, ensure_ascii=False))

# Check what showCodePreview computes
print('\n=== Preview computes ===')
for row in j:
    period = row.get('period')
    input_sum = row.get('ai_called_pass', 0) + row.get('ai_called_fail', 0)
    ai_fail = row.get('ai_called_fail')
    same_ai = row.get('same_as_ai')
    ai_mis = row.get('ai_misjudged')
    print(f"period={period}, input(sum)={input_sum}, ai_called_fail={ai_fail}, same_as_ai={same_ai}, ai_misjudged={ai_mis}")

# Also check the HTML page response to see what web shows
print('\n=== Checking HTML table structure ===')
r4 = client.get('http://10.144.38.11:5000/data_query')
soup4 = BeautifulSoup(r4.text, 'html.parser')
tables = soup4.find_all('table')
for i, t in enumerate(tables):
    print(f'Table {i}: id={t.get("id")}, class={t.get("class")}')
    if t.get('id') == 'result-table' or t.find('th'):
        headers = [th.get_text(strip=True) for th in t.find_all('th')]
        print(f'  Headers: {headers}')
