import logging
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.scraper import WebScraper, detect_source_type
from app.sheets import GoogleSheetsClient
from app.mapper import mapper, DEFAULT_SOURCE_FIELDS, DATA_QUERY_SOURCE_FIELDS
from app import scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LINE_SHEET_FILE = "line_sheet_config.json"
SOURCE_CONFIG_FILE = "source_configs.json"
FEEDER_SOURCE_NAME = "feeder"
FEEDER_EXCEL_FILE = Path("Feeder VA生產數據(1).xlsx")
DATA_QUERY_CODE_SHEETS = [
    {
        "line": "STA5",
        "sheet": "20260525  STA5 行为检测每周数据统计 ",
        "enabled": True,
    },
    {
        "line": "FA",
        "sheet": "20260525 FA 行为检测每周数据统计",
        "enabled": True,
    },
]

DATA_QUERY_CODE_ALIASES = {
    "sat5": ("sat5", "sta5", "aa8"),
    "sta5": ("sat5", "sta5", "aa8"),
    "aa8": ("sat5", "sta5", "aa8"),
    "fa": ("fa",),
}


def data_query_code_aliases(code: str) -> tuple[str, ...]:
    key = (code or "").strip().lower()
    return DATA_QUERY_CODE_ALIASES.get(key, (key,) if key else tuple())


def resolve_data_query_sheet(code: str, code_to_sheet: dict) -> str | None:
    for alias in data_query_code_aliases(code):
        sheet_tab = code_to_sheet.get(alias)
        if sheet_tab:
            return sheet_tab
    if sheets_client:
        for alias in data_query_code_aliases(code):
            sheet_tab = sheets_client.get_sheet_by_code(alias)
            if sheet_tab:
                return sheet_tab
    return None

templates = Jinja2Templates(directory="app/templates")

scraper: WebScraper | None = None
sheets_client: GoogleSheetsClient | None = None
current_sheet_name: str = "Sheet1"
connected_ips: dict = {}  # url -> WebScraper


def sync_data(
    start_time: str = None,
    end_time: str = None,
    line: str = "ALL",
    side: str = "ALL",
    station: str = "ALL",
    target_sheet: str = None,
):
    global scraper, sheets_client
    try:
        logger.info("Starting sync...")

        if scraper is None or not scraper.logged_in:
            return {"status": "error", "message": "Not logged in"}

        if sheets_client is None or sheets_client.gc is None:
            return {"status": "error", "message": "Chưa kết nối Google Sheets"}

        if not start_time or not end_time:
            now = datetime.now()
            start_time = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            end_time = now.strftime("%Y-%m-%d %H:%M:%S")

        sheet_name = target_sheet or current_sheet_name
        headers = sheets_client.get_headers(sheet_name=sheet_name)
        if not headers:
            return {"status": "error", "message": f"Không đọc được headers từ sheet '{sheet_name}'"}

        data = scraper.fetch_summary_data(
            start_time=start_time, end_time=end_time,
            line=line, side=side, station=station,
        )

        date_str = start_time[:10]
        summary_rows = mapper.build_summary_rows(data["summary"], headers)
        total_cells_updated = 0
        summary_results = []
        for row in summary_rows:
            row[0] = date_str
            result = sheets_client.upsert_row_by_date(row, date_str, sheet_name=sheet_name)
            summary_results.append(result)
            if result.get("success"):
                total_cells_updated += result.get("cells_updated", 0)

        detail_rows = mapper.build_detail_rows(data["details"], headers)
        detail_success = 0
        if detail_rows:
            for row in detail_rows:
                result = sheets_client.upsert_row_exact(row, sheet_name=sheet_name)
                if result.get("success"):
                    detail_success += 1

        logger.info(f"Sync completed. Summary: {total_cells_updated} cells updated, {detail_success}/{len(detail_rows)} detail rows appended.")
        if total_cells_updated == 0 and detail_success == 0:
            return {
                "status": "warning",
                "message": "Không có dữ liệu nào được ghi. Kiểm tra: (1) Mapping đã cấu hình đúng cột chưa? (2) Sheet headers có khớp không?",
                "detail_count": 0,
                "summary_cells": 0,
                "sheet_headers": headers,
            }
        return {
            "status": "success",
            "message": f"Đã đồng bộ: {detail_success} dòng chi tiết, {total_cells_updated} ô summary",
            "detail_count": detail_success,
            "summary_cells": total_cells_updated,
        }
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return {"status": "error", "message": str(e)}


def load_line_sheet_config() -> dict:
    if not os.path.exists(LINE_SHEET_FILE):
        return {"mappings": []}
    try:
        with open(LINE_SHEET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"mappings": []}


def save_line_sheet_config(config: dict):
    with open(LINE_SHEET_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_source_configs() -> dict:
    if not os.path.exists(SOURCE_CONFIG_FILE):
        return {}
    try:
        with open(SOURCE_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_source_config(source_url: str, data: dict):
    configs = load_source_configs()
    if source_url not in configs:
        configs[source_url] = {}
    configs[source_url].update(data)
    with open(SOURCE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def get_active_source_url() -> str:
    return scraper.base_url if scraper else ""


def get_active_source_fields() -> list[dict]:
    if scraper and scraper.source_type in ("data_query", "feeder"):
        return DATA_QUERY_SOURCE_FIELDS
    return DEFAULT_SOURCE_FIELDS


def get_sheet_headers_for_active_source(client: GoogleSheetsClient, sheet_name: str) -> list[str]:
    if scraper and scraper.source_type in ("data_query", "feeder"):
        return client.get_behavior_headers(sheet_name=sheet_name)
    return client.get_headers(sheet_name=sheet_name)


def get_default_mappings_for_active_source() -> list[dict]:
    if scraper and scraper.source_type in ("data_query", "feeder"):
        return [
            {"source_key": "period", "dest_col": "Date", "enabled": True, "group": "summary", "label": "Date"},
            {"source_key": "input", "dest_col": "Input", "enabled": True, "group": "summary", "label": "Input"},
            {"source_key": "ai_called_fail", "dest_col": "AI Called Fail", "enabled": True, "group": "summary", "label": "AI Called Fail"},
            {"source_key": "same_as_ai", "dest_col": "Same as AI", "enabled": True, "group": "summary", "label": "Same as AI"},
            {"source_key": "ai_misjudged", "dest_col": "AI Misjudged", "enabled": True, "group": "summary", "label": "AI Misjudged"},
        ]
    return mapper.mappings


def sync_all_lines(start_time: str, end_time: str, side: str = "ALL", station: str = "ALL"):
    config = load_line_sheet_config()
    results = []
    for item in config.get("mappings", []):
        if not item.get("enabled") or not item.get("sheet"):
            continue
        try:
            line = item["line"]
            sheet = item["sheet"]
            logger.info(f"Syncing line '{line}' → sheet '{sheet}'")
            result = sync_data(start_time=start_time, end_time=end_time,
                               line=line, side=side, station=station,
                               target_sheet=sheet)
            results.append({"line": line, "sheet": sheet, "status": result["status"], "message": result.get("message", "")})
        except Exception as e:
            results.append({"line": item["line"], "sheet": item.get("sheet", ""), "status": "error", "message": str(e)})
    return results


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.register_sync_job(sync_data)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Web to Google Sheets Sync Tool", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {
            "source_fields": DEFAULT_SOURCE_FIELDS,
            "dest_headers": mapper.dest_headers,
            "mappings": mapper.mappings,
            "env_username": settings.WEB_USERNAME,
            "env_password": settings.WEB_PASSWORD,
        },
    )

@app.get("/mapping-tree", response_class=HTMLResponse)
async def mapping_tree(request: Request):
    import json
    config = {"mappings": mapper.mappings, "dest_headers": mapper.dest_headers}
    return templates.TemplateResponse(
        request, "mapping_tree.html",
        {"config_json": json.dumps(config, ensure_ascii=False)},
    )


@app.post("/api/login")
async def api_login(username: str = Form(""), password: str = Form(""), web_url: str = Form(None)):
    global scraper, connected_ips
    try:
        s = WebScraper(base_url=web_url)
        s.login(username, password)
        scraper = s
        connected_ips[s.base_url] = s
        return JSONResponse({"status": "success", "message": "Login thành công"})
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=401)


def _parse_number(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r"[^\d.-]", "", str(value))
    if cleaned in ("", "-", ".", "-."):
        return 0
    return int(float(cleaned))


def _excel_serial_to_date(value) -> str | None:
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if not 30000 <= serial <= 60000:
        return None
    return (datetime(1899, 12, 30) + timedelta(days=int(serial))).strftime("%Y-%m-%d")


def _xlsx_rows(path: Path) -> list[list[str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", ns):
                shared_strings.append("".join(t.text or "" for t in item.findall(".//a:t", ns)))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        first_sheet = workbook.find("a:sheets", ns)[0]
        rel_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheet_path = "xl/" + relmap[rel_id].lstrip("/")
        sheet = ET.fromstring(archive.read(sheet_path))

        def cell_value(cell) -> str:
            cell_type = cell.attrib.get("t")
            value = cell.find("a:v", ns)
            inline = cell.find("a:is", ns)
            if cell_type == "s" and value is not None:
                return shared_strings[int(value.text)]
            if cell_type == "inlineStr" and inline is not None:
                return "".join(t.text or "" for t in inline.findall(".//a:t", ns))
            return value.text if value is not None else ""

        rows = []
        for row in sheet.findall(".//a:sheetData/a:row", ns):
            values = []
            for cell in row.findall("a:c", ns):
                ref = cell.attrib.get("r", "")
                col_letters = re.sub(r"\d+", "", ref)
                col_num = 0
                for char in col_letters:
                    col_num = col_num * 26 + ord(char.upper()) - 64
                while len(values) < max(col_num - 1, 0):
                    values.append("")
                values.append(cell_value(cell))
            rows.append(values)
        return rows


def parse_feeder_excel(path: Path = FEEDER_EXCEL_FILE) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file Excel: {path}")
    rows = _xlsx_rows(path)
    row_by_label = {str(row[0]).strip(): row for row in rows if row and str(row[0]).strip()}
    once_row = row_by_label.get("一次 PASS")
    multi_row = row_by_label.get("多次 PASS")
    fail_row = row_by_label.get("Fail")
    if not rows or once_row is None or multi_row is None or fail_row is None:
        raise ValueError("File feeder cần giữ nguyên nhãn: 一次 PASS, 多次 PASS, Fail")

    parsed = []
    for idx, raw_date in enumerate(rows[0]):
        period = _excel_serial_to_date(raw_date)
        if not period:
            continue
        once_pass = _parse_number(once_row[idx] if idx < len(once_row) else "")
        multi_pass = _parse_number(multi_row[idx] if idx < len(multi_row) else "")
        fail = _parse_number(fail_row[idx] if idx < len(fail_row) else "")
        if once_pass == 0 and multi_pass == 0 and fail == 0:
            continue
        parsed.append(
            {
                "period": period,
                "once_pass": once_pass,
                "multi_pass": multi_pass,
                "fail": fail,
            }
        )
    return parsed


def parse_feeder_file(content: bytes, filename: str) -> list[dict]:
    import tempfile
    import csv
    from app.sheets import GoogleSheetsClient
    
    rows = []
    if filename.lower().endswith(".csv"):
        decoded = content.decode("utf-8", errors="replace")
        lines = decoded.splitlines()
        reader = csv.reader(lines)
        rows = [row for row in reader if row]
    else:
        # Excel file (.xlsx)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            rows = _xlsx_rows(tmp_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
                
    if not rows:
        return []
        
    cleaned_rows = []
    for r in rows:
        cleaned_rows.append([str(cell).strip() for cell in r])
        
    # Check if vertical layout (like the original Feeder file)
    row_by_label = {}
    for r in cleaned_rows:
        if r and r[0]:
            row_by_label[r[0]] = r
            
    once_label = next((k for k in row_by_label if k in ("一次 PASS", "一次PASS")), None)
    multi_label = next((k for k in row_by_label if k in ("多次 PASS", "多次PASS")), None)
    fail_label = next((k for k in row_by_label if k in ("Fail", "FAIL", "fail")), None)
    
    if once_label and multi_label and fail_label:
        # Vertical layout
        once_row = row_by_label[once_label]
        multi_row = row_by_label[multi_label]
        fail_row = row_by_label[fail_label]
        
        parsed = []
        # First row is dates
        for idx, raw_date in enumerate(cleaned_rows[0]):
            period = _excel_serial_to_date(raw_date)
            if not period:
                period = GoogleSheetsClient._normalize_date(raw_date)
            if not period:
                continue
            once_pass = _parse_number(once_row[idx] if idx < len(once_row) else "")
            multi_pass = _parse_number(multi_row[idx] if idx < len(multi_row) else "")
            fail = _parse_number(fail_row[idx] if idx < len(fail_row) else "")
            if once_pass == 0 and multi_pass == 0 and fail == 0:
                continue
            parsed.append({
                "period": period,
                "once_pass": once_pass,
                "multi_pass": multi_pass,
                "fail": fail,
            })
        return parsed
        
    # Horizontal layout (like the CSV files)
    # The first row contains headers
    headers = cleaned_rows[0]
    
    # Map headers to indices
    date_idx = next((i for i, h in enumerate(headers) if h.lower() in ("date", "日期")), None)
    once_idx = next((i for i, h in enumerate(headers) if h in ("一次PASS", "一次 PASS", "once_pass")), None)
    multi_idx = next((i for i, h in enumerate(headers) if h in ("多次PASS", "多次 PASS", "multi_pass")), None)
    fail_idx = next((i for i, h in enumerate(headers) if h.lower() in ("fail", "FAIL", "fail")), None)
    
    if date_idx is None or once_idx is None or multi_idx is None or fail_idx is None:
        raise ValueError("File thiếu các cột cần thiết: date/日期, 一次PASS/一次 PASS, 多次PASS/多次 PASS, FAIL/Fail")
        
    parsed = []
    for row in cleaned_rows[1:]:
        if len(row) <= max(date_idx, once_idx, multi_idx, fail_idx):
            continue
        date_val = row[date_idx]
        if not date_val:
            continue
        period = GoogleSheetsClient._normalize_date(date_val)
        if not period:
            period = _excel_serial_to_date(date_val)
        if not period:
            continue
            
        once_pass = _parse_number(row[once_idx])
        multi_pass = _parse_number(row[multi_idx])
        fail = _parse_number(row[fail_idx])
        
        parsed.append({
            "period": period,
            "once_pass": once_pass,
            "multi_pass": multi_pass,
            "fail": fail,
        })
    return parsed


@app.post("/api/login/config")
async def api_login_config():
    global scraper, connected_ips
    urls_to_try = [settings.WEB_BASE_URL]
    for cfg in load_web_configs():
        u = cfg.get("url", "")
        if u and u not in urls_to_try:
            urls_to_try.append(u)

    results = []
    for url in urls_to_try:
        try:
            s = WebScraper(base_url=url)
            s.login()
            scraper = s
            connected_ips[s.base_url] = s
            results.append({"url": s.base_url, "success": True})
        except Exception as e:
            results.append({"url": url, "success": False, "error": str(e)})
    success_count = sum(1 for r in results if r["success"])
    if success_count > 0:
        return JSONResponse({"status": "success", "message": f"Login thành công {success_count}/{len(results)} IP", "results": results})
    return JSONResponse({"status": "error", "message": "Không login được IP nào", "results": results}, status_code=401)


@app.get("/api/connected-ips")
async def api_get_connected_ips():
    global connected_ips
    ips = [{"url": url, "active": url == (getattr(scraper, 'base_url', None) or '')} for url in connected_ips]
    return JSONResponse({"status": "success", "ips": ips})


@app.post("/api/login/disconnect")
async def api_disconnect_ip(url: str = Form(...)):
    global connected_ips, scraper
    if url in connected_ips:
        try:
            connected_ips[url].close()
        except Exception:
            pass
        del connected_ips[url]
    if scraper and scraper.base_url == url:
        scraper = next(iter(connected_ips.values())) if connected_ips else None
    return JSONResponse({"status": "success"})


@app.post("/api/login/switch")
async def api_switch_active_ip(url: str = Form(...)):
    global scraper, sheets_client, current_sheet_name, mapper
    from app.sheets import parse_sheet_url

    if url not in connected_ips:
        return JSONResponse({"status": "error", "message": "IP not connected"}, status_code=400)

    scraper = connected_ips[url]
    cfg = load_source_configs().get(url, {})
    result = {"status": "success", "url": url, "config": cfg}

    sheet_url = cfg.get("sheet_url", "")
    sheet_id = cfg.get("sheet_id", "")

    if sheet_id:
        try:
            settings.GOOGLE_SHEET_ID = sheet_id
            sc = GoogleSheetsClient()
            if not sc.error:
                sheets_client = sc
                sheet_names = sc.get_all_sheet_names()
                if sheet_names:
                    current_sheet_name = sheet_names[0]
                headers = get_sheet_headers_for_active_source(sc, current_sheet_name)
                mapper.set_dest_headers(headers)
                result["sheet_connected"] = True
                result["headers"] = headers
                result["sheet_names"] = sheet_names
                result["sheet_title"] = sc.get_title_row(sheet_name=current_sheet_name)
                result["sheet_url"] = sheet_url
            else:
                sheets_client = None
                result["sheet_error"] = sc.error
        except Exception as e:
            sheets_client = None
            result["sheet_error"] = str(e)
    else:
        sheets_client = None
        mapper.set_dest_headers([])
        result["sheet_connected"] = False

    new_mappings = cfg.get("mappings", [])
    mapper.update_mappings(new_mappings)
    result["mappings"] = new_mappings
    result["mappings_count"] = len([m for m in new_mappings if m.get("active", True)])

    return JSONResponse(result)


@app.post("/api/fetch")
async def api_fetch(
    start_time: str = Form(...),
    end_time: str = Form(...),
    line: str = Form("ALL"),
    side: str = Form("ALL"),
    station: str = Form("ALL"),
):
    global scraper
    if scraper is None or not scraper.logged_in:
        return JSONResponse(
            {"status": "error", "message": "Chưa đăng nhập"}, status_code=401
        )
    try:
        data = scraper.fetch_summary_data(start_time, end_time, line, side, station)
        return JSONResponse({"status": "success", "data": data})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/sheet-url")
async def api_set_sheet_url(url: str = Form(...)):
    global sheets_client, current_sheet_name
    from app.sheets import parse_sheet_url
    sheet_id = parse_sheet_url(url)
    settings.GOOGLE_SHEET_ID = sheet_id
    try:
        sheets_client = GoogleSheetsClient()
        if sheets_client.error:
            return JSONResponse({"status": "error", "message": sheets_client.error}, status_code=400)
        sheet_names = sheets_client.get_all_sheet_names()
        if sheet_names:
            current_sheet_name = sheet_names[0]
        title = sheets_client.get_title_row(sheet_name=current_sheet_name)
        headers = get_sheet_headers_for_active_source(sheets_client, current_sheet_name)
        mapper.set_dest_headers(headers)
        spreadsheet_title = sheets_client.gc.open_by_key(sheet_id).title if sheets_client.gc else ""
        src = get_active_source_url()
        if src:
            save_source_config(src, {"sheet_url": url, "sheet_id": sheet_id})
        return JSONResponse(
            {
                "status": "success",
                "headers": headers,
                "sheet_names": sheet_names,
                "sheet_id": sheet_id,
                "title": title,
                "spreadsheet_title": spreadsheet_title,
            }
        )
    except Exception as e:
        logger.error(f"Sheet error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/sheet-headers")
async def api_sheet_headers(sheet_name: str = "Sheet1"):
    global sheets_client, current_sheet_name
    try:
        if sheets_client is None:
            sheets_client = GoogleSheetsClient()
        if sheets_client.error:
            return JSONResponse({"status": "error", "message": sheets_client.error}, status_code=400)
        current_sheet_name = sheet_name
        headers = get_sheet_headers_for_active_source(sheets_client, sheet_name)
        sheet_names = sheets_client.get_all_sheet_names()
        mapper.set_dest_headers(headers)
        return JSONResponse(
            {"status": "success", "headers": headers, "sheet_names": sheet_names}
        )
    except Exception as e:
        logger.error(f"Sheet headers error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/mappings")
async def api_save_mappings(mappings: list[dict]):
    try:
        mapper.update_mappings(mappings)
        src = get_active_source_url()
        if src:
            save_source_config(src, {"mappings": mappings})
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/mappings")
async def api_get_mappings():
    src = get_active_source_url()
    mappings = get_default_mappings_for_active_source()
    dest_headers = mapper.dest_headers
    if sheets_client is not None and sheets_client.gc is not None:
        dest_headers = get_sheet_headers_for_active_source(sheets_client, current_sheet_name)
        mapper.set_dest_headers(dest_headers)
    if src:
        src_cfg = load_source_configs().get(src, {})
        if "mappings" in src_cfg:
            mappings = src_cfg["mappings"]
    return JSONResponse(
        {
            "status": "success",
            "mappings": mappings,
            "source_fields": get_active_source_fields(),
            "dest_headers": dest_headers,
        }
    )


@app.post("/api/sync")
async def api_sync(
    start_time: str = Form(""),
    end_time: str = Form(""),
    line: str = Form("ALL"),
    side: str = Form("ALL"),
    station: str = Form("ALL"),
):
    st = start_time if start_time else None
    et = end_time if end_time else None
    result = sync_data(start_time=st, end_time=et, line=line, side=side, station=station)
    if result["status"] == "success":
        return JSONResponse(result)
    if result["status"] == "warning":
        return JSONResponse(result, status_code=400)
    return JSONResponse(result, status_code=500)


@app.post("/api/sync-all-lines")
async def api_sync_all_lines(
    start_date: str = Form(...),
    end_date: str = Form(...),
    side: str = Form("ALL"),
    station: str = Form("ALL"),
):
    if scraper is None or not scraper.logged_in:
        return JSONResponse({"status": "error", "message": "Chưa đăng nhập"}, status_code=401)
    start_time = start_date + " 00:00:00"
    end_time = end_date + " 23:59:59"
    results = sync_all_lines(start_time, end_time, side, station)
    ok = sum(1 for r in results if r["status"] == "success")
    err = sum(1 for r in results if r["status"] == "error")
    return JSONResponse({
        "status": "success" if ok > 0 else "error",
        "results": results,
        "summary": f"{ok} line thành công, {err} line thất bại",
    })


@app.post("/api/fetch-by-code")
async def api_fetch_by_code(
    start_date: str = Form(...),
    end_date: str = Form(...),
    code: str = Form(...),
):
    global scraper
    if scraper is None or not scraper.logged_in:
        return JSONResponse({"status": "error", "message": "Chưa đăng nhập"}, status_code=401)
    if scraper.source_type != "data_query":
        return JSONResponse({"status": "error", "message": "Nguồn này không hỗ trợ fetch-by-code"}, status_code=400)
    try:
        data = scraper.fetch_by_code(start_date, end_date, code)
        return JSONResponse({"status": "success", "data": data})
    except Exception as e:
        logger.error(f"fetch-by-code error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/sync-weekly")
async def api_sync_weekly(
    start_date: str = Form(...),
    end_date: str = Form(...),
    code: str = Form("sta5"),
    sheet_tab: str = Form("Sheet1"),
):
    global scraper, sheets_client
    if scraper is None or not scraper.logged_in:
        return JSONResponse({"status": "error", "message": "Chưa đăng nhập"}, status_code=401)
    if sheets_client is None or sheets_client.gc is None:
        return JSONResponse({"status": "error", "message": "Chưa kết nối Google Sheets"}, status_code=400)
    try:
        rows = scraper.fetch_by_code(start_date, end_date, code)
        if not rows:
            return JSONResponse({"status": "warning", "message": "Không có dữ liệu từ web nguồn"})
        if scraper.source_type == "data_query" and (not sheet_tab or sheet_tab == "Sheet1"):
            src_cfg = load_source_configs().get(get_active_source_url(), {})
            configured = src_cfg.get("line_sheet_mappings") or DATA_QUERY_CODE_SHEETS
            code_to_sheet = {
                item.get("line", "").lower(): item.get("sheet", "")
                for item in configured
                if item.get("enabled", True) and item.get("line") and item.get("sheet")
            }
            sheet_tab = resolve_data_query_sheet(code, code_to_sheet) or sheet_tab
        result = sheets_client.write_weekly(rows, start_date, end_date, sheet_name=sheet_tab)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"sync-weekly error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


def ensure_feeder_context():
    global scraper, sheets_client, current_sheet_name, connected_ips
    if scraper is None or scraper.source_type != "feeder":
        scraper = WebScraper(base_url=FEEDER_SOURCE_NAME)
        scraper.login()
        connected_ips[scraper.base_url] = scraper

    cfg = load_source_configs().get(FEEDER_SOURCE_NAME, {})
    sheet_id = cfg.get("sheet_id")
    if sheet_id and (sheets_client is None or sheets_client.sheet_id != sheet_id):
        settings.GOOGLE_SHEET_ID = sheet_id
        sheets_client = GoogleSheetsClient()
        if sheets_client.error:
            raise RuntimeError(sheets_client.error)
        sheet_names = sheets_client.get_all_sheet_names()
        if sheet_names:
            current_sheet_name = sheet_names[0]


@app.post("/api/feeder/preview")
async def api_feeder_preview(file: UploadFile = File(None)):
    try:
        if file is not None:
            filename = file.filename
            content = await file.read()
            rows = parse_feeder_file(content, filename)
        else:
            rows = parse_feeder_excel()
        return JSONResponse({"status": "success", "rows": rows, "count": len(rows)})
    except Exception as e:
        logger.error(f"feeder preview error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/feeder/sync")
async def api_feeder_sync(
    file: UploadFile = File(None),
    sheet_url: str = Form(""),
    sheet_tab: str = Form("")
):
    global sheets_client, current_sheet_name
    from collections import defaultdict
    try:
        if file is not None:
            filename = file.filename
            content = await file.read()
            rows = parse_feeder_file(content, filename)
        else:
            rows = parse_feeder_excel()
            
        if not rows:
            return JSONResponse({"status": "warning", "message": "Không có dữ liệu feeder hợp lệ"})
            
        from app.sheets import parse_sheet_url
        if sheet_url:
            sheet_id = parse_sheet_url(sheet_url)
            settings.GOOGLE_SHEET_ID = sheet_id
            sheets_client = GoogleSheetsClient()
            if sheets_client.error:
                return JSONResponse({"status": "error", "message": sheets_client.error}, status_code=400)
        else:
            ensure_feeder_context()
            
        if sheets_client is None or sheets_client.gc is None:
            return JSONResponse({"status": "error", "message": "Chưa kết nối Google Sheets"}, status_code=400)
            
        target_tab = sheet_tab
        if not target_tab and sheet_url:
            match = re.search(r"gid=(\d+)", sheet_url)
            if match:
                gid = match.group(1)
                try:
                    sh = sheets_client.gc.open_by_key(settings.GOOGLE_SHEET_ID)
                    for ws in sh.worksheets():
                        if str(ws.id) == gid:
                            target_tab = ws.title
                            break
                except Exception as e:
                    logger.warning(f"Failed to find worksheet by GID: {e}")
            if not target_tab:
                try:
                    names = sheets_client.get_all_sheet_names()
                    if names:
                        target_tab = names[0]
                except Exception:
                    target_tab = current_sheet_name or "Sheet1"
        elif not target_tab:
            target_tab = current_sheet_name or "Sheet1"
            
        # Group rows by week, and write them sequentially week-by-week
        grouped_data = defaultdict(list)
        for r in rows:
            dt = datetime.strptime(r["period"], "%Y-%m-%d")
            year, week, _ = dt.isocalendar()
            grouped_data[(year, week)].append(r)
            
        sorted_week_keys = sorted(grouped_data.keys())
        results = []
        max_date = max(r["period"] for r in rows) if rows else None
        for year, week in sorted_week_keys:
            week_rows = sorted(grouped_data[(year, week)], key=lambda x: x["period"])
            res = sheets_client.write_feeder_weekly(week_rows, sheet_name=target_tab, last_date=max_date)
            results.append(res)
            import time
            time.sleep(2)
            
        successes = [r for r in results if r.get("status") == "success"]
        if successes:
            return JSONResponse({
                "status": "success",
                "message": f"Đã đồng bộ thành công {len(successes)}/{len(results)} tuần dữ liệu vào sheet '{target_tab}'"
            })
        else:
            msg = results[0].get("message", "Đồng bộ thất bại") if results else "Không có dữ liệu tuần nào được ghi"
            return JSONResponse({"status": "error", "message": msg}, status_code=500)
            
    except Exception as e:
        logger.error(f"feeder sync error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/sync-codes")
async def api_sync_codes(
    start_date: str = Form(...),
    end_date: str = Form(...),
    codes: str = Form("sta5,fa"),
):
    global scraper, sheets_client
    if scraper is None or not scraper.logged_in:
        return JSONResponse({"status": "error", "message": "Chưa đăng nhập"}, status_code=401)
    if sheets_client is None or sheets_client.gc is None:
        return JSONResponse({"status": "error", "message": "Chưa kết nối Google Sheets"}, status_code=400)
    code_list = [c.strip() for c in codes.split(",")]
    if scraper.source_type == "data_query":
        src_cfg = load_source_configs().get(get_active_source_url(), {})
        configured = src_cfg.get("line_sheet_mappings") or DATA_QUERY_CODE_SHEETS
        code_to_sheet = {
            item.get("line", "").lower(): item.get("sheet", "")
            for item in configured
            if item.get("enabled", True) and item.get("line") and item.get("sheet")
        }
    else:
        code_to_sheet = {}
    results = {}
    for code in code_list:
        if not code:
            continue
        try:
            rows = scraper.fetch_by_code(start_date, end_date, code)
            sheet_tab = resolve_data_query_sheet(code, code_to_sheet)
            if not sheet_tab:
                sheet_tab = code
                try:
                    sheets_client.gc.open_by_key(sheets_client.sheet_id).add_worksheet(title=code, rows=100, cols=20)
                except Exception:
                    pass
            result = sheets_client.write_weekly(rows, start_date, end_date, sheet_name=sheet_tab)
            results[code] = result
        except Exception as e:
            results[code] = {"status": "error", "message": str(e)}
    return JSONResponse({"status": "success", "results": results})


@app.get("/api/line-sheet-config")
async def api_get_line_sheet_config():
    config = load_line_sheet_config()
    src = get_active_source_url()
    if scraper and scraper.source_type == "data_query":
        config = {"mappings": DATA_QUERY_CODE_SHEETS}
    if src:
        src_cfg = load_source_configs().get(src, {})
        if "line_sheet_mappings" in src_cfg:
            config = {"mappings": src_cfg["line_sheet_mappings"]}
    sheet_names = sheets_client.get_all_sheet_names() if sheets_client and sheets_client.gc else []
    return JSONResponse({"status": "success", "config": config, "sheet_names": sheet_names})


@app.post("/api/line-sheet-config")
async def api_save_line_sheet_config(mappings: list[dict]):
    config = {"mappings": mappings}
    src = get_active_source_url()
    if src:
        save_source_config(src, {"line_sheet_mappings": mappings})
    else:
        save_line_sheet_config(config)
    return JSONResponse({"status": "success"})


@app.get("/api/sync-status")
async def api_sync_status():
    return JSONResponse(
        {
            "status": "success",
            "logged_in": scraper is not None and scraper.logged_in,
            "sheets_connected": sheets_client is not None,
            "active_mappings": len(mapper.get_active_mappings()),
            "scheduler_interval_hours": settings.SCHEDULER_INTERVAL_HOURS,
            "connected_ips": list(connected_ips.keys()),
            "active_ip": scraper.base_url if scraper else "",
        }
    )


# ============== .ENV & SHEET CONFIG ==============

def update_env_file(key: str, value: str):
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@app.post("/api/login/save-env")
async def api_save_login_env(username: str = Form(...), password: str = Form(...)):
    try:
        update_env_file("WEB_USERNAME", username)
        update_env_file("WEB_PASSWORD", password)
        return JSONResponse({"status": "success", "message": "Saved to .env"})
    except Exception as e:
        logger.error(f"Save env failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


SHEET_CONFIG_FILE = "sheet_config.json"


def load_sheet_configs() -> list:
    if not os.path.exists(SHEET_CONFIG_FILE):
        return []
    try:
        with open(SHEET_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_sheet_configs(configs: list):
    with open(SHEET_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


@app.get("/api/sheet-configs")
async def api_get_sheet_configs():
    configs = load_sheet_configs()
    return JSONResponse({"status": "success", "configs": configs})


@app.post("/api/sheet-configs")
async def api_save_sheet_config(name: str = Form(""), url: str = Form(...), sheet_id: str = Form("")):
    configs = load_sheet_configs()
    existing = [c for c in configs if c.get("url") == url]
    if existing:
        existing[0]["name"] = name or existing[0].get("name", "")
        existing[0]["sheet_id"] = sheet_id or existing[0].get("sheet_id", "")
    else:
        configs.append({"name": name or "Untitled", "url": url, "sheet_id": sheet_id})
    save_sheet_configs(configs)
    return JSONResponse({"status": "success", "configs": configs})


@app.post("/api/sheet-configs/delete")
async def api_delete_sheet_config(url: str = Form(...)):
    configs = load_sheet_configs()
    configs = [c for c in configs if c.get("url") != url]
    save_sheet_configs(configs)
    return JSONResponse({"status": "success", "configs": configs})


WEB_CONFIG_FILE = "web_configs.json"


def load_web_configs() -> list:
    if not os.path.exists(WEB_CONFIG_FILE):
        return []
    try:
        with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_web_configs(configs: list):
    with open(WEB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


@app.get("/api/web-configs")
async def api_get_web_configs():
    configs = load_web_configs()
    return JSONResponse({"status": "success", "configs": configs})


@app.post("/api/web-configs")
async def api_save_web_config(url: str = Form(...), name: str = Form("")):
    configs = load_web_configs()
    existing = [c for c in configs if c.get("url") == url]
    if existing:
        existing[0]["name"] = name or existing[0].get("name", url)
    else:
        configs.append({"name": name or url, "url": url})
    save_web_configs(configs)
    return JSONResponse({"status": "success", "configs": configs})


@app.post("/api/web-configs/delete")
async def api_delete_web_config(url: str = Form(...)):
    configs = load_web_configs()
    configs = [c for c in configs if c.get("url") != url]
    save_web_configs(configs)
    return JSONResponse({"status": "success", "configs": configs})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.APP_DEBUG,
    )
