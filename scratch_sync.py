import csv
import sys
import os
from datetime import datetime
from collections import defaultdict

# Add current directory to path so we can import app components
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app.config import settings
from app.sheets import GoogleSheetsClient, parse_sheet_url

def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
        
    # Allow passing sheet ID/URL, GID, and CSV file path as arguments
    # Usage: python scratch_sync.py [sheet_url_or_id] [gid] [csv_file_path]
    sheet_id = "1DzCuwvOf9u275Bsg54RgJ5gIYnum_-9MjL-9B7rhkL4"
    target_gid = "608597169"
    csv_file_path = "0511~0526.csv"
    
    if len(sys.argv) > 1:
        sheet_id = parse_sheet_url(sys.argv[1])
    if len(sys.argv) > 2:
        target_gid = sys.argv[2]
    if len(sys.argv) > 3:
        csv_file_path = sys.argv[3]
        
    print(f"Target Sheet ID: {sheet_id}")
    print(f"Target GID: {target_gid}")
    print(f"CSV File Path: {csv_file_path}")
    print(f"Reading data from {csv_file_path}...")
    
    if not os.path.exists(csv_file_path):
        print(f"Error: File {csv_file_path} not found.")
        sys.exit(1)
        
    rows = []
    with open(csv_file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("date", "").strip()
            if not date_str:
                continue
            
            try:
                once_pass = int(row.get("一次PASS") or 0)
                multi_pass = int(row.get("多次PASS") or 0)
                fail = int(row.get("FAIL") or 0)
            except ValueError as e:
                print(f"Warning: could not parse row {row}: {e}")
                continue
            
            rows.append({
                "period": date_str,
                "once_pass": once_pass,
                "multi_pass": multi_pass,
                "fail": fail
            })
            
    print(f"Found {len(rows)} valid data rows in CSV.")
    if not rows:
        print("Error: No data rows found.")
        sys.exit(1)
    
    # Group rows by ISO year and week
    grouped_data = defaultdict(list)
    for r in rows:
        dt = datetime.strptime(r["period"], "%Y-%m-%d")
        year, week, _ = dt.isocalendar()
        grouped_data[(year, week)].append(r)
        
    # Sort weeks chronologically
    sorted_week_keys = sorted(grouped_data.keys())
    print(f"Grouped into {len(sorted_week_keys)} weeks.")
    
    # Initialize Google Sheets client
    print("Connecting to Google Sheets...")
    settings.GOOGLE_SHEET_ID = sheet_id
    client = GoogleSheetsClient()
    if client.error:
        print(f"Error initializing GoogleSheetsClient: {client.error}")
        sys.exit(1)
        
    # Find worksheet name by gid
    try:
        sh = client.gc.open_by_key(sheet_id)
        target_ws_name = None
        for ws in sh.worksheets():
            if str(ws.id) == target_gid:
                target_ws_name = ws.title
                break
                
        if not target_ws_name:
            print(f"Error: Worksheet with GID {target_gid} not found in spreadsheet.")
            sys.exit(1)
            
        print(f"Found target worksheet: '{target_ws_name}' (GID: {target_gid})")
        
        # Write week by week
        max_date = max(r["period"] for r in rows) if rows else None
        for year, week in sorted_week_keys:
            week_rows = sorted(grouped_data[(year, week)], key=lambda x: x["period"])
            week_dates = [r["period"] for r in week_rows]
            print(f"Syncing week {week} of {year} ({len(week_rows)} days: {week_dates[0]} to {week_dates[-1]})...")
            
            res = client.write_feeder_weekly(week_rows, sheet_name=target_ws_name, last_date=max_date)
            if res.get("status") == "success":
                print(f"  Success: {res.get('message')}")
            else:
                print(f"  Failed: {res.get('message')}")
            import time
            time.sleep(3)
                
        print("Data upload completed successfully!")
        
    except Exception as e:
        import traceback
        print("An error occurred:")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
