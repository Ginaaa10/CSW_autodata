import json
import os
from typing import Optional

MAPPING_FILE = "mapping_config.json"

DEFAULT_SOURCE_FIELDS = [
    {
        "key": "totalpcs",
        "label": "Total (Tổng số)",
        "group": "summary",
        "note": "Tổng số sản phẩm kiểm tra trong kỳ",
    },
    {
        "key": "failpcs",
        "label": "NG (Số lỗi)",
        "group": "summary",
        "note": "Số sản phẩm bị lỗi (Failed)",
    },
    {
        "key": "passrate",
        "label": "Pass Rate (Tỷ lệ đạt)",
        "group": "summary",
        "note": "Tỷ lệ sản phẩm đạt yêu cầu (phần trăm)",
    },
    {
        "key": "ntfrate",
        "label": "NTF Rate",
        "group": "summary",
        "note": "Số lỗi NTF và tỷ lệ (VD: 0 pcs 0.0%)",
    },
    {
        "key": "leakagerate",
        "label": "Leakage Rate",
        "group": "summary",
        "note": "Số lỗi Leakage và tỷ lệ (VD: 0 pcs 0.0%)",
    },
    {
        "key": "SN",
        "label": "SN (Mã sản phẩm)",
        "group": "detail",
        "note": "Số serial của sản phẩm",
    },
    {
        "key": "ai_time",
        "label": "AI Time (Giờ AI)",
        "group": "detail",
        "note": "Thời gian AI kiểm tra",
    },
    {
        "key": "pic_date_time",
        "label": "Pic Time (Giờ chụp)",
        "group": "detail",
        "note": "Thời gian chụp ảnh",
    },
    {
        "key": "ai_predict",
        "label": "AI Result (KQ AI)",
        "group": "detail",
        "note": "Kết quả dự đoán của AI (PASS/FAIL)",
    },
    {
        "key": "line",
        "label": "Line (Dây chuyền)",
        "group": "detail",
        "note": "Mã dây chuyền sản xuất (TG4-1, AK3-1...)",
    },
    {
        "key": "side",
        "label": "Side (Mặt)",
        "group": "detail",
        "note": "Mặt kiểm tra (R = Right, L = Left)",
    },
    {
        "key": "station",
        "label": "Station (Trạm)",
        "group": "detail",
        "note": "Trạm kiểm tra (1, 2, 3, 4...)",
    },
    {
        "key": "Report_Error",
        "label": "Report Error (Báo lỗi)",
        "group": "detail",
        "note": "Nội dung báo lỗi từ người dùng",
    },
    {
        "key": "Report_User",
        "label": "Report User (Người BC)",
        "group": "detail",
        "note": "Người báo cáo lỗi",
    },
    {
        "key": "pic_url",
        "label": "Picture URL (Link ảnh)",
        "group": "detail",
        "note": "Đường dẫn ảnh chụp sản phẩm",
    },
]

DATA_QUERY_SOURCE_FIELDS = [
    {
        "key": "period",
        "label": "Date",
        "group": "summary",
        "note": "Ngày/thời gian thống kê từ web 38.11",
    },
    {
        "key": "input",
        "label": "Input",
        "group": "summary",
        "note": "ai_called_pass + ai_called_fail",
    },
    {
        "key": "ai_called_fail",
        "label": "AI Called Fail",
        "group": "summary",
        "note": "Số lượng AI gọi Fail",
    },
    {
        "key": "same_as_ai",
        "label": "Same as AI",
        "group": "summary",
        "note": "Số lượng IPQC xác nhận giống AI",
    },
    {
        "key": "ai_misjudged",
        "label": "AI Misjudged",
        "group": "summary",
        "note": "Số lượng AI phán đoán sai",
    },
]


class FieldMapper:
    def __init__(self):
        self.mappings: list[dict] = []
        self.dest_headers: list[str] = []
        self.load()

    @property
    def source_fields(self) -> list[dict]:
        return DEFAULT_SOURCE_FIELDS

    def set_dest_headers(self, headers: list[str]):
        self.dest_headers = headers

    def load(self):
        if os.path.exists(MAPPING_FILE):
            with open(MAPPING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.mappings = data.get("mappings", [])
                self.dest_headers = data.get("dest_headers", [])

    def save(self):
        with open(MAPPING_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "mappings": self.mappings,
                    "dest_headers": self.dest_headers,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    def update_mappings(self, mappings: list[dict]):
        self.mappings = mappings
        self.save()

    def get_active_mappings(self) -> list[dict]:
        return [m for m in self.mappings if m.get("enabled", False)]

    def build_row_for_headers(
        self, headers: list[str], source_data: dict, active_mappings: list[dict]
    ) -> list:
        """
        Build a row list ordered by sheet headers.
        Each value is placed in the column position matching its dest_col mapping.
        """
        row = [""] * len(headers)
        for mapping in active_mappings:
            src_key = mapping["source_key"]
            dest_col = mapping.get("dest_col", "")
            if dest_col in headers:
                idx = headers.index(dest_col)
                row[idx] = source_data.get(src_key, "")
        return row

    def build_summary_rows(
        self, summary_data: dict, headers: list[str]
    ) -> list[list]:
        active = self.get_active_summary_mappings()
        if not active or not headers:
            return []
        return [self.build_row_for_headers(headers, summary_data, active)]

    def build_detail_rows(
        self, details: list[dict], headers: list[str]
    ) -> list[list]:
        active = self.get_active_detail_mappings()
        if not active or not headers:
            return []
        rows = []
        for item in details:
            row = self.build_row_for_headers(headers, item, active)
            if any(v != "" for v in row):
                rows.append(row)
        return rows

    def get_active_summary_mappings(self) -> list[dict]:
        return [m for m in self.mappings if m.get("enabled") and m.get("group") == "summary"]

    def get_active_detail_mappings(self) -> list[dict]:
        return [m for m in self.mappings if m.get("enabled") and m.get("group") == "detail"]


mapper = FieldMapper()
