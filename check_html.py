import httpx, json
from bs4 import BeautifulSoup

client = httpx.Client(timeout=30, verify=False)
r = client.get('http://10.144.38.11:5000/data_query')
soup = BeautifulSoup(r.text, 'html.parser')
csrf = soup.find('input', {'name': 'csrf_token'})
csrf_val = csrf['value'] if csrf else ''
r2 = client.post('http://10.144.38.11:5000/login', data={'csrf_token': csrf_val, 'username': 'gina_ruan', 'password': '12qazxcvbnm,./'}, follow_redirects=True)
print('Login:', r2.status_code)

# GET to data_query with params
r3 = client.get('http://10.144.38.11:5000/data_query', params={
    'start_date': '2026-06-01', 'end_date': '2026-06-07',
    'model': 'STA5', 'line': 'all', 'station': 'all',
    'side': 'all', 'num': 'all', 'interval': 'day'
})
print('GET status:', r3.status_code)
soup3 = BeautifulSoup(r3.text, 'html.parser')
tables = soup3.find_all('table')
for i, t in enumerate(tables):
    print(f'Table {i}: id={t.get("id")}, class={t.get("class")}')
    headers = [th.get_text(strip=True) for th in t.find_all('th')]
    print(f'  Headers: {headers}')
    for tr in t.find_all('tr')[:3]:
        cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
        print(f'  Row: {cells}')
