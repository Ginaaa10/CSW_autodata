import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    WEB_USERNAME: str = os.getenv("WEB_USERNAME", "")
    WEB_PASSWORD: str = os.getenv("WEB_PASSWORD", "")
    WEB_BASE_URL: str = os.getenv("WEB_BASE_URL", "http://10.144.38.15:9100")
    WEB_LOGIN_URL: str = os.getenv("WEB_LOGIN_URL", "http://10.144.38.15:9100")
    WEB_DATA_URL: str = os.getenv("WEB_DATA_URL", "http://10.144.38.15:9100/DataAnalysis/NTF/")
    WEB_GET_SUMMARY_API: str = os.getenv("WEB_GET_SUMMARY_API", "http://10.144.38.15:9100/DataAnalysis/GetSummaryData/")

    WEB2_BASE_URL: str = os.getenv("WEB2_BASE_URL", "http://10.144.38.11:5000")
    WEB2_USERNAME: str = os.getenv("WEB2_USERNAME", "")
    WEB2_PASSWORD: str = os.getenv("WEB2_PASSWORD", "")

    GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "1oWH3A6e3bMJ9hDbNc93gU-xHbKrsNN_NCO17icBiapY")
    GOOGLE_SHEET_GID: str = os.getenv("GOOGLE_SHEET_GID", "1194957140")
    GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    GOOGLE_SHEET2_ID: str = os.getenv("GOOGLE_SHEET2_ID", "")

    APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "change-this-secret-key")
    SCHEDULER_INTERVAL_HOURS: int = int(os.getenv("SCHEDULER_INTERVAL_HOURS", "1"))
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    APP_DEBUG: bool = os.getenv("APP_DEBUG", "true").lower() == "true"

settings = Settings()
