import os
import re
import copy
from datetime import datetime, timedelta
import logging
import gspread
from gspread.utils import PasteType
from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
YEAR_SUFFIX = "\u5e74"
MONTH_SUFFIX = "\u6708"

HEADER_ROW = 2
DATA_START_ROW = 3


def parse_sheet_url(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return url.strip()


def _execute_with_retry(func, name, *args, **kwargs):
    import time
    from gspread.exceptions import APIError, WorksheetNotFound, SpreadsheetNotFound
    retries = 8
    delay = 5
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except (APIError, OSError, Exception) as e:
            if isinstance(e, (WorksheetNotFound, SpreadsheetNotFound)):
                raise
            if isinstance(e, APIError):
                if not ((hasattr(e, 'code') and e.code == 429) or "429" in str(e)):
                    raise
            logger.warning(f"Connection or API 429 error on {name} (attempt {i+1}/{retries}): {e}. Retrying in {delay} seconds...")
            time.sleep(delay)
            delay *= 2
    return func(*args, **kwargs)


class RetryingClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                res = _execute_with_retry(attr, f"client.{name}", *args, **kwargs)
                import gspread
                if isinstance(res, gspread.Spreadsheet):
                    return RetryingSpreadsheet(res)
                return res
            return wrapper
        return attr

class RetryingSpreadsheet:
    def __init__(self, sh):
        self._sh = sh

    def __getattr__(self, name):
        attr = getattr(self._sh, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                res = _execute_with_retry(attr, f"spreadsheet.{name}", *args, **kwargs)
                import gspread
                if isinstance(res, gspread.Worksheet):
                    return RetryingWorksheet(res)
                elif isinstance(res, list) and res and isinstance(res[0], gspread.Worksheet):
                    return [RetryingWorksheet(w) for w in res]
                return res
            return wrapper
        return attr

class RetryingWorksheet:
    def __init__(self, ws):
        self._ws = ws

    @property
    def spreadsheet(self):
        return RetryingSpreadsheet(self._ws.spreadsheet)

    def __getattr__(self, name):
        attr = getattr(self._ws, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                res = _execute_with_retry(attr, f"worksheet.{name}", *args, **kwargs)
                import gspread
                if isinstance(res, gspread.Worksheet):
                    return RetryingWorksheet(res)
                return res
            return wrapper
        return attr


class GoogleSheetsClient:
    def __init__(self):
        self.sheet_id = settings.GOOGLE_SHEET_ID
        self.gc = None
        self.error = None
        self._authenticate()

    def _authenticate(self):
        creds_file = settings.GOOGLE_CREDENTIALS_FILE
        if not os.path.exists(creds_file):
            self.error = (
                f"File '{creds_file}' không tồn tại. "
                "Tải file JSON từ Google Cloud Console và đặt vào thư mục D:\\auto data"
            )
            logger.error(self.error)
            return
        try:
            self.gc = RetryingClient(gspread.service_account(filename=creds_file))
            logger.info("Google Sheets connected")
        except Exception as e:
            self.error = f"Lỗi kết nối: {e}"
            logger.error(self.error)

    def _ws(self, sheet_name: str):
        sh = self.gc.open_by_key(self.sheet_id)
        # Note: sh is a RetryingSpreadsheet, so sh.worksheet returns a RetryingWorksheet
        return sh.worksheet(sheet_name)

    def get_title_row(self, sheet_name: str = "Sheet1") -> str:
        if not self.gc:
            return ""
        try:
            vals = self._ws(sheet_name).row_values(1)
            return vals[0] if vals else ""
        except Exception:
            return ""

    def get_headers(self, sheet_name: str = "Sheet1") -> list:
        if not self.gc:
            return []
        try:
            return self._ws(sheet_name).row_values(HEADER_ROW)
        except gspread.exceptions.APIError as e:
            self.error = f"API error: {e}"
            logger.error(self.error)
            return []
        except Exception as e:
            self.error = f"Sheet read error: {e}"
            logger.error(self.error)
            return []

    def get_behavior_headers(self, sheet_name: str = "Sheet1") -> list:
        """Headers for 38.11 sheets are fixed labels in column A, rows 1-5."""
        if not self.gc:
            return []
        try:
            return [v for v in self._ws(sheet_name).col_values(1)[:5] if v]
        except gspread.exceptions.APIError as e:
            self.error = f"Lỗi API: {e}"
            logger.error(self.error)
            return []
        except Exception as e:
            self.error = f"Lỗi đọc sheet: {e}"
            logger.error(self.error)
            return []

    def get_all_sheet_names(self) -> list:
        if not self.gc:
            return []
        try:
            sh = self.gc.open_by_key(self.sheet_id)
            return [ws.title for ws in sh.worksheets()]
        except Exception as e:
            self.error = f"Lỗi đọc sheet names: {e}"
            logger.error(self.error)
            return []

    @staticmethod
    def _normalize_date(val: str) -> str | None:
        """Try to parse a date string into YYYY-MM-DD format"""
        cleaned = str(val).strip().strip("'\"")
        fmts = [
            "%Y-%m-%d", "%Y/%m/%d",
            "%m/%d/%Y", "%m-%d-%Y",
            "%d/%m/%Y", "%d-%m-%Y",
            "%Y.%m.%d",
            "%m/%d/%y", "%m-%d-%y",
            "%d/%m/%y", "%d-%m-%y",
        ]
        for fmt in fmts:
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_md(val: str) -> tuple | None:
        """Extract month, day from string like '5/15', '05-15', '5月15' etc."""
        m = re.search(r'(\d{1,2})\s*[/\-\.月]\s*(\d{1,2})', str(val))
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None

    @staticmethod
    def _dates_match(cell_val: str, search_date: str) -> bool:
        """Check if two date strings represent the same date (flexible formats)"""
        if search_date in cell_val:
            return True
        search_norm = GoogleSheetsClient._normalize_date(search_date)
        if search_norm:
            cell_norm = GoogleSheetsClient._normalize_date(cell_val)
            if cell_norm and cell_norm == search_norm:
                return True
        # Fallback: compare month/day only
        # search_date is always "YYYY-MM-DD", extract M/D directly
        if len(search_date) >= 10 and search_date[4] == '-':
            try:
                search_m = int(search_date[5:7])
                search_d = int(search_date[8:10])
            except ValueError:
                return False
            cell_md = GoogleSheetsClient._extract_md(cell_val)
            if cell_md and cell_md == (search_m, search_d):
                return True
        return False

    def find_row_by_date(self, date_str: str, sheet_name: str = "Sheet1") -> int:
        """Tìm dòng có ngày khớp trong cột A, trả về số dòng hoặc None"""
        if not self.gc:
            return None
        try:
            ws = self._ws(sheet_name)
            dates = ws.col_values(1)
            for i, val in enumerate(dates):
                if i < DATA_START_ROW - 1:
                    continue
                if not val:
                    continue
                if self._dates_match(str(val).strip(), date_str):
                    return i + 1
            return None
        except Exception:
            return None

    def upsert_row_by_date(
        self, values: list, date_str: str, sheet_name: str = "Sheet1"
    ) -> dict:
        """Ghi dữ liệu vào đúng dòng theo ngày (cột A).
           Return: {"success": bool, "action": "updated"/"appended"/"error", "row_num": int|None, "error": str|None}"""
        if not self.gc:
            return {"success": False, "action": "error", "error": "Not connected to Google Sheets"}
        try:
            ws = self._ws(sheet_name)
            row_num = self.find_row_by_date(date_str, sheet_name)
            has_data = any(v for v in values if v != "")

            if row_num:
                existing = ws.row_values(row_num)
                updated_cells = 0
                for i, v in enumerate(values):
                    if v and i != 0:
                        old = existing[i] if i < len(existing) else ""
                        if str(v) != str(old):
                            ws.update_cell(row_num, i + 1, v)
                            updated_cells += 1
                logger.info(f"Updated row {row_num} for date {date_str} ({updated_cells} cells changed)")
                return {"success": True, "action": "updated", "row_num": row_num, "cells_updated": updated_cells}
            elif has_data:
                ws.append_row(values, table_range=f"A{DATA_START_ROW}")
                logger.info(f"Appended new row for date {date_str}")
                return {"success": True, "action": "appended", "row_num": None}
            else:
                return {"success": False, "action": "skipped", "error": "No data to write"}
        except Exception as e:
            logger.error(f"Lỗi upsert row: {e}")
            return {"success": False, "action": "error", "error": str(e)}

    def append_row(self, values: list, sheet_name: str = "Sheet1") -> dict:
        """Append a row, return result dict."""
        if not self.gc:
            return {"success": False, "error": "Not connected"}
        try:
            ws = self._ws(sheet_name)
            ws.append_row(values, table_range=f"A{DATA_START_ROW}")
            return {"success": True, "action": "appended"}
        except Exception as e:
            logger.error(f"Lỗi append row: {e}")
            return {"success": False, "error": str(e)}

    def upsert_row_exact(self, values: list, sheet_name: str = "Sheet1") -> dict:
        """Update an identical existing row if found, otherwise append."""
        if not self.gc:
            return {"success": False, "error": "Not connected"}
        try:
            ws = self._ws(sheet_name)
            width = max(len(values), 1)
            range_end = self._col_letter(width)
            existing_rows = ws.get(f"A{DATA_START_ROW}:{range_end}")
            target = ["" if v is None else str(v) for v in values]
            for offset, row in enumerate(existing_rows, start=DATA_START_ROW):
                candidate = ["" if v is None else str(v) for v in row[:width]]
                if len(candidate) < width:
                    candidate.extend([""] * (width - len(candidate)))
                if candidate == target:
                    ws.update(range_name=f"A{offset}:{range_end}{offset}", values=[values], raw=False)
                    return {"success": True, "action": "updated", "row_num": offset}
            ws.append_row(values, table_range=f"A{DATA_START_ROW}")
            return {"success": True, "action": "appended"}
        except Exception as e:
            logger.error(f"upsert_row_exact error: {e}")
            return {"success": False, "error": str(e)}

    def close(self):
        pass

    def get_sheet_by_code(self, code: str) -> str | None:
        """Find sheet tab name matching a data code (sta5, tg4, etc.)"""
        if not self.gc:
            return None
        try:
            names = self.get_all_sheet_names()
            for n in names:
                if n.lower() == code.lower():
                    return n
                if code.lower() in n.lower():
                    return n
            return None
        except Exception:
            return None

    def write_weekly(self, rows: list, start_date: str, end_date: str, sheet_name: str = "Sheet1") -> dict:
        if rows and isinstance(rows[0], dict) and "period" in rows[0]:
            return self.write_behavior_weekly(rows, sheet_name=sheet_name)
        """
        Write scraped data rows into weekly structure.
        Header Row 1: Week# | Date1 | Date2 | ... | DateN
        Data Row 2+: values grouped by week.
        """
        if not self.gc:
            return {"status": "error", "message": "Not connected"}
        if not rows:
            return {"status": "warning", "message": "No data rows"}
        try:
            ws = self._ws(sheet_name)
            all_dates = list(dict.fromkeys(r.get("日期", r.get("Date", r.get("date", ""))) for r in rows))
            all_dates = [d for d in all_dates if d]
            if not all_dates:
                all_dates = self._generate_date_range(start_date, end_date)
            all_dates = sorted(set(all_dates))
            headers = ["Week#"] + all_dates
            existing = ws.row_values(1)
            if existing != headers:
                ws.update(range_name="A1", values=[headers])
            value_rows = {}
            for r in rows:
                date = r.get("日期", r.get("Date", r.get("date", "")))
                for k, v in r.items():
                    if k in ("日期", "Date", "date"):
                        continue
                    key = f"{k}"
                    if key not in value_rows:
                        value_rows[key] = {d: "" for d in all_dates}
                    if date in value_rows[key]:
                        value_rows[key][date] = str(v)
            data_to_write = []
            row_idx = 2
            for field_key, date_vals in value_rows.items():
                week_label = self._calc_week_label(start_date, end_date, all_dates)
                data_row = [week_label]
                for d in all_dates:
                    data_row.append(date_vals.get(d, ""))
                existing_row = ws.row_values(row_idx) if len(ws.col_values(1)) >= row_idx else []
                if len(existing_row) != len(data_row) or existing_row != data_row:
                    ws.update(range_name=f"A{row_idx}", values=[data_row])
                data_to_write.append(data_row)
                row_idx += 1
            return {
                "status": "success",
                "message": f"Đã ghi {len(data_to_write)} dòng, {len(all_dates)} ngày",
                "dates": all_dates,
                "fields": list(value_rows.keys()),
                "rows_written": len(data_to_write),
            }
        except Exception as e:
            logger.error(f"write_weekly error: {e}")
            return {"status": "error", "message": str(e)}

    def write_behavior_weekly(self, rows: list[dict], sheet_name: str = "Sheet1") -> dict:
        """
        Write 38.11 behavior data into the sheet layout:
        A1:A5 = Date/Input/AI Called Fail/Same as AI/AI Misjudged.
        B+ columns = one date per column. New data is appended after the
        current rightmost data column, and each new column copies the layout
        and formulas from the previous data column before values are filled.
        """
        if not self.gc:
            return {"status": "error", "message": "Not connected"}
        if not rows:
            return {"status": "warning", "message": "No data rows"}
        try:
            ws = self._ws(sheet_name)
            sorted_rows = sorted(
                [r for r in rows if r.get("period")],
                key=lambda r: str(r.get("period", "")),
            )
            if not sorted_rows:
                return {"status": "warning", "message": "No rows with period"}

            dates = [str(r.get("period", "")) for r in sorted_rows]
            first_date = datetime.strptime(dates[0], "%Y-%m-%d")
            week_label = f"WK{first_date.isocalendar().week}"
            year_label, month_label = self._assigned_year_month_label(first_date)
            input_values = [
                int(float(r.get("ai_called_pass") or 0)) + int(float(r.get("ai_called_fail") or 0))
                for r in sorted_rows
            ]
            row1 = ws.row_values(1)
            last_col = self._last_non_empty_col(row1)
            last_week_col, last_week_label = self._last_week_col(row1)
            last_year_col = self._last_label_col(row1, rf"\d{{4}}(?:{YEAR_SUFFIX})?")
            last_month_col = self._last_label_col(row1, rf"\d{{4}}\d{{2}}|\d{{1,2}}{MONTH_SUFFIX}")
            year_col = self._find_year_col(row1, year_label)
            month_col = self._find_month_col(row1, month_label, year_col)
            same_open_week = last_week_label == week_label and last_week_col and last_week_col < last_col

            new_year = not year_col
            new_month = not month_col
            if same_open_week:
                week_col = last_week_col
                date_start_col = last_col + 1
                template_col = last_col
            else:
                next_col = last_col + 1
                if new_year:
                    year_col = next_col
                    next_col += 1
                if new_month:
                    month_col = next_col
                    next_col += 1
                week_col = next_col
                date_start_col = week_col + 1
                template_col = last_col if last_col > 1 else None

            date_end_col = date_start_col + len(sorted_rows) - 1
            if ws.col_count < date_end_col:
                ws.add_cols(date_end_col - ws.col_count)

            template_rows = max(len(ws.col_values(1)), 7)
            if new_year:
                year_template_col = last_year_col or template_col
                if year_template_col:
                    source = f"{self._col_letter(year_template_col)}1:{self._col_letter(year_template_col)}{template_rows}"
                    dest = f"{self._col_letter(year_col)}1"
                    ws.copy_range(source, dest, paste_type=PasteType.normal)

            if new_month:
                month_template_col = last_month_col or template_col
                if month_template_col:
                    source = f"{self._col_letter(month_template_col)}1:{self._col_letter(month_template_col)}{template_rows}"
                    dest = f"{self._col_letter(month_col)}1"
                    ws.copy_range(source, dest, paste_type=PasteType.normal)

            if not same_open_week:
                wk_template_col = last_week_col or template_col
                if wk_template_col:
                    source = f"{self._col_letter(wk_template_col)}1:{self._col_letter(wk_template_col)}{template_rows}"
                    dest = f"{self._col_letter(week_col)}1"
                    ws.copy_range(source, dest, paste_type=PasteType.normal)

            for offset in range(len(sorted_rows)):
                target_col = date_start_col + offset
                if template_col:
                    source = f"{self._col_letter(template_col)}1:{self._col_letter(template_col)}{template_rows}"
                    dest = f"{self._col_letter(target_col)}1"
                    ws.copy_range(source, dest, paste_type=PasteType.normal)
                template_col = target_col

            date_start_letter = self._col_letter(date_start_col)
            date_end_letter = self._col_letter(date_end_col)
            values = [
                dates,
                input_values,
                [r.get("ai_called_fail", "") for r in sorted_rows],
                [r.get("same_as_ai", "") for r in sorted_rows],
                [r.get("ai_misjudged", "") for r in sorted_rows],
            ]
            ws.update(
                range_name=f"{date_start_letter}1:{date_end_letter}5",
                values=values,
                raw=False,
            )

            week_letter = self._col_letter(week_col)
            formula_start_letter = self._col_letter(week_col + 1)
            week_formulas = [
                [week_label],
                [f"=SUM({formula_start_letter}2:{date_end_letter}2)"],
                [f"=SUM({formula_start_letter}3:{date_end_letter}3)"],
                [f"=SUM({formula_start_letter}4:{date_end_letter}4)"],
                [f"=SUM({formula_start_letter}5:{date_end_letter}5)"],
            ]
            ws.update(range_name=f"{week_letter}1:{week_letter}5", values=week_formulas, raw=False)
            if template_rows >= 7:
                ws.update(
                    range_name=f"{week_letter}6:{week_letter}7",
                    values=[[f"={week_letter}5/{week_letter}2"], [f"={week_letter}4/{week_letter}2"]],
                    raw=False,
                )

            updated_row1 = list(row1)
            if len(updated_row1) < date_end_col:
                updated_row1.extend([""] * (date_end_col - len(updated_row1)))
            if new_year:
                updated_row1[year_col - 1] = year_label
            if new_month:
                updated_row1[month_col - 1] = month_label
            if not same_open_week:
                updated_row1[week_col - 1] = week_label
            for offset, date_value in enumerate(dates):
                updated_row1[date_start_col - 1 + offset] = date_value

            self._update_month_summary(ws, updated_row1, month_col, template_rows)
            self._update_year_summary(ws, updated_row1, year_col, template_rows)

            self._post_process_behavior_columns(ws, week_col, date_start_col, date_end_col)
            return {
                "status": "success",
                "message": f"Đã ghi {len(sorted_rows)} ngày vào {sheet_name}: {month_label} {week_label} ({date_start_letter}:{date_end_letter})",
                "dates": dates,
                "week": week_label,
                "month": month_label,
                "year": year_label,
                "rows_written": 5,
                "cols_written": len(sorted_rows),
                "year_col": self._col_letter(year_col),
                "month_col": self._col_letter(month_col),
                "week_col": week_letter,
                "start_col": date_start_letter,
                "end_col": date_end_letter,
            }
        except Exception as e:
            logger.error(f"write_behavior_weekly error: {e}")
            return {"status": "error", "message": str(e)}

    def write_feeder_weekly(self, rows: list[dict], sheet_name: str = "Sheet1", last_date: str = None) -> dict:
        """
        Write feeder rows into the same year/month/week/date column layout as
        behavior sheets, with A1:A4 = Date/一次 PASS/多次 PASS/Fail.
        """
        if not self.gc:
            return {"status": "error", "message": "Not connected"}
        if not rows:
            return {"status": "warning", "message": "No data rows"}
        try:
            ws = self._ws(sheet_name)
            sorted_rows = sorted(
                [r for r in rows if r.get("period")],
                key=lambda r: str(r.get("period", "")),
            )
            if not sorted_rows:
                return {"status": "warning", "message": "No rows with period"}

            # Pad all 7 days of the ISO week (Monday to Sunday)
            first_date = datetime.strptime(sorted_rows[0]["period"], "%Y-%m-%d")
            monday = first_date - timedelta(days=first_date.weekday())
            week_dates = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
            
            # Limit to last_date if provided
            if last_date:
                week_dates = [d for d in week_dates if d <= last_date]
                
            row_by_date = {r["period"]: r for r in sorted_rows}
            
            padded_rows = []
            for d in week_dates:
                if d in row_by_date:
                    padded_rows.append(row_by_date[d])
                else:
                    padded_rows.append({
                        "period": d,
                        "once_pass": "",
                        "multi_pass": "",
                        "fail": ""
                    })
            sorted_rows = padded_rows

            dates = [str(r.get("period", "")) for r in sorted_rows]
            first_date = datetime.strptime(dates[0], "%Y-%m-%d")
            week_label = f"WK{first_date.isocalendar().week}"
            year_label, month_label = self._assigned_year_month_label(first_date)
            row1 = ws.row_values(1)
            last_col = self._last_non_empty_col(row1)
            last_week_col, last_week_label = self._last_week_col(row1)
            last_year_col = self._last_label_col(row1, rf"\d{{4}}(?:{YEAR_SUFFIX})?")
            last_month_col = self._last_label_col(row1, rf"\d{{4}}\d{{2}}|\d{{1,2}}{MONTH_SUFFIX}")
            year_col = self._find_year_col(row1, year_label)
            month_col = self._find_month_col(row1, month_label, year_col)
            same_open_week = last_week_label == week_label and last_week_col and last_week_col < last_col

            new_year = not year_col
            new_month = not month_col
            if same_open_week:
                week_col = last_week_col
                date_start_col = last_col + 1
                template_col = last_col
            else:
                next_col = last_col + 1
                if new_year:
                    year_col = next_col
                    next_col += 1
                if new_month:
                    month_col = next_col
                    next_col += 1
                week_col = next_col
                date_start_col = week_col + 1
                template_col = last_col if last_col > 1 else None

            date_end_col = date_start_col + len(sorted_rows) - 1
            if ws.col_count < date_end_col:
                ws.add_cols(date_end_col - ws.col_count)

            template_rows = max(len(ws.col_values(1)), 4)
            if new_year:
                year_template_col = last_year_col or template_col
                if year_template_col:
                    source = f"{self._col_letter(year_template_col)}1:{self._col_letter(year_template_col)}{template_rows}"
                    ws.copy_range(source, f"{self._col_letter(year_col)}1", paste_type=PasteType.normal)

            if new_month:
                month_template_col = last_month_col or template_col
                if month_template_col:
                    source = f"{self._col_letter(month_template_col)}1:{self._col_letter(month_template_col)}{template_rows}"
                    ws.copy_range(source, f"{self._col_letter(month_col)}1", paste_type=PasteType.normal)

            if not same_open_week:
                wk_template_col = last_week_col or template_col
                if wk_template_col:
                    source = f"{self._col_letter(wk_template_col)}1:{self._col_letter(wk_template_col)}{template_rows}"
                    ws.copy_range(source, f"{self._col_letter(week_col)}1", paste_type=PasteType.normal)

            for offset in range(len(sorted_rows)):
                target_col = date_start_col + offset
                if template_col:
                    source = f"{self._col_letter(template_col)}1:{self._col_letter(template_col)}{template_rows}"
                    ws.copy_range(source, f"{self._col_letter(target_col)}1", paste_type=PasteType.normal)
                template_col = target_col

            date_start_letter = self._col_letter(date_start_col)
            date_end_letter = self._col_letter(date_end_col)
            values = [
                dates,
                [r.get("once_pass", "") for r in sorted_rows],
                [r.get("multi_pass", "") for r in sorted_rows],
                [r.get("fail", "") for r in sorted_rows],
            ]
            ws.update(range_name=f"{date_start_letter}1:{date_end_letter}4", values=values, raw=False)

            week_letter = self._col_letter(week_col)
            formula_start_letter = self._col_letter(week_col + 1)
            week_formulas = [
                [week_label],
                [f"=SUM({formula_start_letter}2:{date_end_letter}2)"],
                [f"=SUM({formula_start_letter}3:{date_end_letter}3)"],
                [f"=SUM({formula_start_letter}4:{date_end_letter}4)"],
            ]
            ws.update(range_name=f"{week_letter}1:{week_letter}4", values=week_formulas, raw=False)

            updated_row1 = list(row1)
            if len(updated_row1) < date_end_col:
                updated_row1.extend([""] * (date_end_col - len(updated_row1)))
            if new_year:
                updated_row1[year_col - 1] = year_label
            if new_month:
                updated_row1[month_col - 1] = month_label
            if not same_open_week:
                updated_row1[week_col - 1] = week_label
            for offset, date_value in enumerate(dates):
                updated_row1[date_start_col - 1 + offset] = date_value

            self._update_month_summary_rows(ws, updated_row1, month_col, template_rows, 3)
            self._update_year_summary_rows(ws, updated_row1, year_col, template_rows, 3)
            self._post_process_behavior_columns(ws, week_col, date_start_col, date_end_col)
            return {
                "status": "success",
                "message": f"Đã ghi {len(sorted_rows)} ngày feeder vào {sheet_name}: {month_label} {week_label} ({date_start_letter}:{date_end_letter})",
                "dates": dates,
                "week": week_label,
                "month": month_label,
                "year": year_label,
                "rows_written": 4,
                "cols_written": len(sorted_rows),
                "start_col": date_start_letter,
                "end_col": date_end_letter,
            }
        except Exception as e:
            logger.error(f"write_feeder_weekly error: {e}")
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _next_behavior_data_col(ws) -> int:
        row = ws.row_values(1)
        return GoogleSheetsClient._last_non_empty_col(row) + 1

    @staticmethod
    def _last_non_empty_col(row: list) -> int:
        last_data_col = 1
        for idx, val in enumerate(row, start=1):
            if str(val).strip():
                last_data_col = idx
        return max(last_data_col, 1)

    @staticmethod
    def _last_week_col(row: list) -> tuple[int | None, str | None]:
        for idx in range(len(row), 0, -1):
            val = str(row[idx - 1]).strip().upper()
            if re.fullmatch(r"WK\d+", val):
                return idx, val
        return None, None

    @staticmethod
    def _find_label_col(row: list, label: str) -> int | None:
        for idx, val in enumerate(row, start=1):
            if str(val).strip() == label:
                return idx
        return None

    @staticmethod
    def _normalize_year_label(label: str) -> str | None:
        match = re.fullmatch(r"(\d{4})(?:\s*" + re.escape(YEAR_SUFFIX) + r")?", str(label).strip())
        return match.group(1) if match else None

    @staticmethod
    def _normalize_month_label(label: str) -> tuple[str | None, int | None]:
        val = str(label).strip()
        match = re.fullmatch(r"(\d{4})(\d{2})", val)
        if match:
            return match.group(1), int(match.group(2))
        match = re.fullmatch(r"(\d{1,2})\s*" + re.escape(MONTH_SUFFIX), val)
        if match:
            return None, int(match.group(1))
        return None, None

    @classmethod
    def _find_year_col(cls, row: list, year_label: str) -> int | None:
        target_year = cls._normalize_year_label(year_label)
        if not target_year:
            return cls._find_label_col(row, year_label)
        for idx, val in enumerate(row, start=1):
            if cls._normalize_year_label(str(val).strip()) == target_year:
                return idx
        return None

    @classmethod
    def _find_month_col(cls, row: list, month_label: str, year_col: int | None = None) -> int | None:
        target_year, target_month = cls._normalize_month_label(month_label)
        if target_month is None:
            return cls._find_label_col(row, month_label)

        candidates = []
        for idx, val in enumerate(row, start=1):
            val_year, val_month = cls._normalize_month_label(str(val).strip())
            if val_month != target_month:
                continue
            if val_year and target_year and val_year != target_year:
                continue
            candidates.append(idx)

        if year_col:
            for idx in candidates:
                if idx <= year_col:
                    continue
                between = row[year_col:idx - 1]
                if not any(cls._normalize_year_label(str(val).strip()) for val in between):
                    return idx
        if target_year:
            for idx in candidates:
                val_year, _ = cls._normalize_month_label(str(row[idx - 1]).strip())
                if val_year == target_year:
                    return idx
            return None
        return candidates[0] if candidates else None

    @staticmethod
    def _last_label_col(row: list, pattern: str) -> int | None:
        for idx in range(len(row), 0, -1):
            if re.fullmatch(pattern, str(row[idx - 1]).strip()):
                return idx
        return None

    @staticmethod
    def _assigned_year_month_label(date_value: datetime) -> tuple[str, str]:
        monday = date_value - timedelta(days=date_value.weekday())
        days = [monday + timedelta(days=i) for i in range(7)]
        current_month_days = sum(1 for day in days if day.month == monday.month)
        assigned = monday if current_month_days >= 3 else monday + timedelta(days=7)
        return f"{assigned.year}", f"{assigned.year}{assigned.month:02d}"

    @staticmethod
    def _label_columns_between(row: list, start_col: int, label_pattern: str, stop_pattern: str) -> list[int]:
        cols = []
        for idx in range(start_col + 1, len(row) + 1):
            val = str(row[idx - 1]).strip()
            if re.fullmatch(stop_pattern, val):
                break
            if re.fullmatch(label_pattern, val):
                cols.append(idx)
        return cols

    def _update_month_summary(self, ws, row1: list, month_col: int, template_rows: int):
        self._update_month_summary_rows(ws, row1, month_col, template_rows, 4)

    @staticmethod
    def _is_complete_week(row1: list, week_col: int) -> bool:
        date_count = 0
        for idx in range(week_col, len(row1)):
            val = str(row1[idx]).strip()
            if not val:
                continue
            if re.fullmatch(r"WK\d+", val) or re.fullmatch(rf"\d{{4}}\d{{2}}|\d{{1,2}}{MONTH_SUFFIX}|\d{{4}}(?:{YEAR_SUFFIX})?", val):
                break
            date_count += 1
        return date_count >= 7

    def _update_month_summary_rows(self, ws, row1: list, month_col: int, template_rows: int, data_row_count: int):
        month_letter = self._col_letter(month_col)
        week_cols = self._label_columns_between(
            row1,
            month_col,
            r"WK\d+",
            rf"\d{{4}}\d{{2}}|\d{{1,2}}{MONTH_SUFFIX}|\d{{4}}(?:{YEAR_SUFFIX})?",
        )
        # Filter to keep only complete weeks
        week_cols = [col for col in week_cols if self._is_complete_week(row1, col)]
        
        if not week_cols:
            formulas = [[str(row1[month_col - 1])]] + [["0"] for _ in range(data_row_count)]
            ws.update(range_name=f"{month_letter}1:{month_letter}{data_row_count + 1}", values=formulas, raw=False)
            return
            
        week_refs = ";".join(self._col_letter(col) for col in week_cols)
        formulas = [[str(row1[month_col - 1])]]
        for row_num in range(2, data_row_count + 2):
            refs = ";".join(f"{self._col_letter(col)}{row_num}" for col in week_cols)
            formulas.append([f"=SUM({refs})"])
        ws.update(range_name=f"{month_letter}1:{month_letter}{data_row_count + 1}", values=formulas, raw=False)
        if template_rows >= 7:
            ws.update(
                range_name=f"{month_letter}6:{month_letter}7",
                values=[[f"={month_letter}5/{month_letter}2"], [f"={month_letter}4/{month_letter}2"]],
                raw=False,
            )

    def _update_year_summary(self, ws, row1: list, year_col: int, template_rows: int):
        self._update_year_summary_rows(ws, row1, year_col, template_rows, 4)

    def _update_year_summary_rows(self, ws, row1: list, year_col: int, template_rows: int, data_row_count: int):
        year_letter = self._col_letter(year_col)
        month_cols = self._label_columns_between(
            row1,
            year_col,
            rf"\d{{4}}\d{{2}}|\d{{1,2}}{MONTH_SUFFIX}",
            rf"\d{{4}}(?:{YEAR_SUFFIX})?",
        )
        if not month_cols:
            return
        formulas = [[str(row1[year_col - 1])]]
        for row_num in range(2, data_row_count + 2):
            refs = ";".join(f"{self._col_letter(col)}{row_num}" for col in month_cols)
            formulas.append([f"=SUM({refs})"])
        ws.update(range_name=f"{year_letter}1:{year_letter}{data_row_count + 1}", values=formulas, raw=False)
        if template_rows >= 7:
            ws.update(
                range_name=f"{year_letter}6:{year_letter}7",
                values=[[f"={year_letter}5/{year_letter}2"], [f"={year_letter}4/{year_letter}2"]],
                raw=False,
            )

    def _sync_to_benefit_sheet(self, first_sheet_name: str, month_label: str, month_letter: str):
        if not self.gc:
            return
        try:
            try:
                ws = self._ws("效益计算")
            except Exception:
                logger.warning("Worksheet '效益计算' not found.")
                return
                
            row2 = ws.row_values(2)
            month_idx = None
            ttl_idx = None
            for idx, val in enumerate(row2, start=1):
                val_str = str(val).strip()
                if val_str == month_label:
                    month_idx = idx
                    break
                if val_str == "TTL":
                    ttl_idx = idx
                    
            ref_formula = f"='{first_sheet_name}'!{month_letter}5"
            
            if month_idx:
                ws.update(range_name=f"{self._col_letter(month_idx)}3", values=[[ref_formula]], raw=False)
                logger.info(f"Updated formula in 效益计算 column {self._col_letter(month_idx)} to {ref_formula}")
            elif ttl_idx:
                template_col = ttl_idx - 1
                template_letter = self._col_letter(template_col)
                new_col = ttl_idx
                new_letter = self._col_letter(new_col)
                
                sheet_id = ws.id
                body = {
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": new_col - 1,
                                    "endIndex": new_col
                                },
                                "inheritFromBefore": True
                            }
                        }
                    ]
                }
                ws.spreadsheet.batch_update(body)
                
                source_range = f"{template_letter}1:{template_letter}10"
                target_range = f"{new_letter}1"
                ws.copy_range(source_range, target_range, paste_type=PasteType.normal)
                
                row4_template = ws.cell(4, template_col, value_render_option='FORMULA').value or ""
                dec_sep = "," if "," in str(row4_template) else "."
                row4_formula = f"={new_letter}3*0{dec_sep}21"
                row5_formula = f"={new_letter}3*0{dec_sep}2"
                row6_val = ws.cell(6, template_col).value or "36.5"
                
                ws.update(
                    range_name=f"{new_letter}2:{new_letter}6",
                    values=[
                        [month_label],
                        [ref_formula],
                        [row4_formula],
                        [row5_formula],
                        [row6_val]
                    ],
                    raw=False
                )
                logger.info(f"Inserted new column in 效益计算 at {new_letter} for month {month_label}")
        except Exception as e:
            logger.error(f"Error syncing to benefit sheet: {e}")

    def _post_process_behavior_columns(self, ws, week_col: int, date_start_col: int, date_end_col: int):
        sheet_id = ws.id
        resize_and_chart_requests = [
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": week_col - 1,
                        "endIndex": date_end_col,
                    }
                }
            },
        ]
        resize_and_chart_requests.extend(self._chart_update_requests(ws, date_end_col))
        try:
            ws.spreadsheet.batch_update({"requests": resize_and_chart_requests})
        except Exception as e:
            logger.warning(f"resize/chart update failed: {e}")

        try:
            ws.spreadsheet.batch_update({
                "requests": [{
                "addDimensionGroup": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": date_start_col - 1,
                        "endIndex": date_end_col,
                    }
                }
            }]})
        except Exception as e:
            logger.warning(f"column grouping skipped: {e}")

    def _chart_update_requests(self, ws, end_col: int) -> list[dict]:
        try:
            metadata = ws.spreadsheet.fetch_sheet_metadata(params={"includeGridData": "false"})
            sheet_meta = next(
                s for s in metadata.get("sheets", [])
                if s.get("properties", {}).get("sheetId") == ws.id
            )
            requests = []
            for chart in sheet_meta.get("charts", []):
                spec = copy.deepcopy(chart.get("spec", {}))
                self._extend_chart_ranges(spec, ws.id, end_col)
                requests.append({"updateChartSpec": {"chartId": chart["chartId"], "spec": spec}})
            return requests
        except Exception as e:
            logger.warning(f"chart metadata update skipped: {e}")
            return []

    def _extend_chart_ranges(self, node, sheet_id: int, end_col: int):
        if isinstance(node, dict):
            if {"sheetId", "startColumnIndex", "endColumnIndex"}.issubset(node.keys()) and node.get("sheetId") == sheet_id:
                node["endColumnIndex"] = max(int(node.get("endColumnIndex", 0)), end_col)
            for value in node.values():
                self._extend_chart_ranges(value, sheet_id, end_col)
        elif isinstance(node, list):
            for item in node:
                self._extend_chart_ranges(item, sheet_id, end_col)

    @staticmethod
    def _col_letter(col_num: int) -> str:
        letters = ""
        while col_num:
            col_num, rem = divmod(col_num - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def _generate_date_range(self, start_date: str, end_date: str) -> list:
        fmts = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"]
        start = None
        for f in fmts:
            try:
                start = datetime.strptime(start_date, f)
                break
            except ValueError:
                continue
        end = None
        for f in fmts:
            try:
                end = datetime.strptime(end_date, f)
                break
            except ValueError:
                continue
        if not start or not end:
            return [start_date, end_date]
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return dates

    @staticmethod
    def _calc_week_label(start_date: str, end_date: str, all_dates: list) -> str:
        if not all_dates:
            return ""
        try:
            d = datetime.strptime(all_dates[0], "%Y-%m-%d")
            iso = d.isocalendar()
            return f"W{iso[1]}"
        except (ValueError, IndexError):
            try:
                d = datetime.strptime(all_dates[0], "%d/%m/%Y")
                iso = d.isocalendar()
                return f"W{iso[1]}"
            except (ValueError, IndexError):
                return f"Week ({all_dates[0]} - {all_dates[-1]})"
