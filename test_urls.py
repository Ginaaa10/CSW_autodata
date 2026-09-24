import sys; sys.path.insert(0, ".")
from app.scraper import WebScraper

# Test with default URL
s = WebScraper(base_url=None)
print(f"Default scraper: type={s.source_type}")
print(f"  login_url={s.login_url}")
print(f"  data_url={s.data_url}")
print(f"  api_url={s.api_url}")

# Test with explicit default URL
s2 = WebScraper(base_url="http://10.144.38.15:9100")
print(f"\nExplicit default: type={s2.source_type}")
print(f"  login_url={s2.login_url}")
print(f"  data_url={s2.data_url}")
print(f"  api_url={s2.api_url}")

# Test with 38.11
s3 = WebScraper(base_url="http://10.144.38.11:5000")
print(f"\n38.11:5000: type={s3.source_type}")
print(f"  data_query_url={s3.data_query_url}")
print(f"  login_post_url={s3.login_post_url}")

# Test with non-standard 38.15 URL
s4 = WebScraper(base_url="http://10.144.38.15:9100/Report/NTF")
print(f"\nNon-standard 38.15: type={s4.source_type}")
print(f"  login_url={s4.login_url}")
print(f"  data_url={s4.data_url}")
print(f"  api_url={s4.api_url}")

s.close(); s2.close(); s3.close(); s4.close()
