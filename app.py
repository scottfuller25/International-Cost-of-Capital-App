"""
International Cost of Capital App
=================================

Streamlit app that estimates a project's cost of equity, cost of debt and
WACC in U.S. dollars and in a selected country's currency.

Live data sources (latest available):
  * Industry unlevered betas ...... Aswath Damodaran, betas.xls (U.S. industries)
  * BBB (Baa2) USD default spread . Aswath Damodaran, ratings.html (large non-financial firms)
  * Corporate income tax rates .... OECD Corporate Tax Statistics (combined statutory rate)
  * 10-year government bond yields  Trading Economics (tradingeconomics.com/bonds)

Method (same tax rate, debt share and equity risk premium in both currencies):
  D/E               = d / (1 - d)
  Levered beta      = Bu x [1 + (1 - t) x D/E]                       (Hamada)
  Cost of equity    = rf + levered beta x ERP                         (CAPM)
  Foreign BBB spread (auto) = USD spread x (1 + rf_foreign) / (1 + rf_US)
  Pre-tax cost of debt      = rf + BBB spread
  After-tax cost of debt    = pre-tax cost of debt x (1 - t)
  WACC              = (1 - d) x cost of equity + d x after-tax cost of debt

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import io
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_REPORT_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
    "full-report/statutory-corporate-income-tax-rates_440f9286.html"
)
OECD_API_URL = "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,/all"
TE_BONDS_URL = "https://tradingeconomics.com/bonds"
TE_COUNTRY_URL = "https://tradingeconomics.com/{slug}/government-bond-yield"

DEFAULT_INDUSTRY = "Software (System & Application)"
US = "United States"
DEFAULT_ERP = 6.5
DEFAULT_DEBT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30

# name: (ISO3 for OECD, Trading Economics slug, currency, fallback 2024 combined CIT rate %)
# Fallback rates are only used if the live OECD request fails. They are the 2024 combined
# statutory rates in OECD Corporate Tax Statistics 2024, Figure 2.1 (Taiwan is not covered
# by the OECD report; its 20% rate is the general statutory rate).
COUNTRIES: dict[str, tuple[str, str, str, float]] = {
    "United States": ("USA", "united-states", "USD", 25.63),
    "Argentina": ("ARG", "argentina", "ARS", 30.0),
    "Australia": ("AUS", "australia", "AUD", 30.0),
    "Austria": ("AUT", "austria", "EUR", 23.0),
    "Belgium": ("BEL", "belgium", "EUR", 25.0),
    "Brazil": ("BRA", "brazil", "BRL", 34.0),
    "Canada": ("CAN", "canada", "CAD", 26.14),
    "Chile": ("CHL", "chile", "CLP", 27.0),
    "China": ("CHN", "china", "CNY", 25.0),
    "Colombia": ("COL", "colombia", "COP", 35.0),
    "Czech Republic": ("CZE", "czech-republic", "CZK", 21.0),
    "Denmark": ("DNK", "denmark", "DKK", 22.0),
    "Egypt": ("EGY", "egypt", "EGP", 22.5),
    "Finland": ("FIN", "finland", "EUR", 20.0),
    "France": ("FRA", "france", "EUR", 25.83),
    "Germany": ("DEU", "germany", "EUR", 29.93),
    "Greece": ("GRC", "greece", "EUR", 22.0),
    "Hong Kong": ("HKG", "hong-kong", "HKD", 16.5),
    "Hungary": ("HUN", "hungary", "HUF", 9.0),
    "India": ("IND", "india", "INR", 25.17),
    "Indonesia": ("IDN", "indonesia", "IDR", 22.0),
    "Ireland": ("IRL", "ireland", "EUR", 12.5),
    "Israel": ("ISR", "israel", "ILS", 23.0),
    "Italy": ("ITA", "italy", "EUR", 27.81),
    "Japan": ("JPN", "japan", "JPY", 29.74),
    "Malaysia": ("MYS", "malaysia", "MYR", 24.0),
    "Mexico": ("MEX", "mexico", "MXN", 30.0),
    "Netherlands": ("NLD", "netherlands", "EUR", 25.8),
    "New Zealand": ("NZL", "new-zealand", "NZD", 28.0),
    "Nigeria": ("NGA", "nigeria", "NGN", 30.0),
    "Norway": ("NOR", "norway", "NOK", 22.0),
    "Peru": ("PER", "peru", "PEN", 29.5),
    "Philippines": ("PHL", "philippines", "PHP", 25.0),
    "Poland": ("POL", "poland", "PLN", 19.0),
    "Portugal": ("PRT", "portugal", "EUR", 31.5),
    "Singapore": ("SGP", "singapore", "SGD", 17.0),
    "South Africa": ("ZAF", "south-africa", "ZAR", 27.0),
    "South Korea": ("KOR", "south-korea", "KRW", 26.4),
    "Spain": ("ESP", "spain", "EUR", 25.0),
    "Sweden": ("SWE", "sweden", "SEK", 20.6),
    "Switzerland": ("CHE", "switzerland", "CHF", 19.61),
    "Taiwan": ("TWN", "taiwan", "TWD", 20.0),
    "Thailand": ("THA", "thailand", "THB", 20.0),
    "Turkey": ("TUR", "turkey", "TRY", 20.0),
    "United Kingdom": ("GBR", "united-kingdom", "GBP", 25.0),
    "Vietnam": ("VNM", "vietnam", "VND", 20.0),
}
SLUG_TO_COUNTRY = {v[1]: k for k, v in COUNTRIES.items()}


# Built-in copy of Damodaran's betas.xls ("Industry Averages", date updated January 05, 2026).
# Used only if the live download fails: (industry, unlevered beta, number of firms).
BETAS_SNAPSHOT_DATE = "January 05, 2026"
BETAS_SNAPSHOT: list[tuple[str, float, int]] = [
    ('Advertising', 0.930086, 52),
    ('Aerospace/Defense', 0.846669, 79),
    ('Air Transport', 0.704050, 23),
    ('Apparel', 0.757982, 35),
    ('Auto & Truck', 1.269033, 33),
    ('Auto Parts', 1.021714, 35),
    ('Bank (Money Center)', 0.341059, 15),
    ('Banks (Regional)', 0.286516, 568),
    ('Beverage (Alcoholic)', 0.613172, 14),
    ('Beverage (Soft)', 0.555671, 27),
    ('Broadcasting', 0.286362, 24),
    ('Brokerage & Investment Banking', 0.580849, 32),
    ('Building Materials', 0.930162, 41),
    ('Business & Consumer Services', 0.773588, 155),
    ('Cable TV', 0.352392, 9),
    ('Chemical (Basic)', 0.580033, 29),
    ('Chemical (Diversified)', 0.366552, 4),
    ('Chemical (Specialty)', 0.792227, 59),
    ('Coal & Related Energy', 1.016535, 16),
    ('Computer Services', 0.915510, 64),
    ('Computers/Peripherals', 1.305062, 36),
    ('Construction Supplies', 1.016068, 40),
    ('Diversified', 0.788930, 20),
    ('Drugs (Biotechnology)', 1.034129, 496),
    ('Drugs (Pharmaceutical)', 0.886211, 228),
    ('Education', 0.660196, 32),
    ('Electrical Equipment', 1.147795, 112),
    ('Electronics (Consumer & Office)', 0.829853, 8),
    ('Electronics (General)', 0.897365, 114),
    ('Engineering/Construction', 1.094912, 48),
    ('Entertainment', 0.737184, 92),
    ('Environmental & Waste Services', 0.814469, 53),
    ('Farming/Agriculture', 0.813020, 35),
    ('Financial Svcs. (Non-bank & Insurance)', 0.318882, 176),
    ('Food Processing', 0.457418, 78),
    ('Food Wholesalers', 0.640882, 13),
    ('Furn/Home Furnishings', 0.624788, 27),
    ('Green & Renewable Energy', 0.463301, 15),
    ('Healthcare Products', 0.829063, 204),
    ('Healthcare Support Services', 0.688916, 104),
    ('Heathcare Information and Technology', 0.991301, 115),
    ('Homebuilding', 0.784960, 30),
    ('Hospitals/Healthcare Facilities', 0.551730, 31),
    ('Hotel/Gaming', 0.832251, 63),
    ('Household Products', 0.717816, 110),
    ('Information Services', 0.737177, 15),
    ('Insurance (General)', 0.563640, 21),
    ('Insurance (Life)', 0.427156, 20),
    ('Insurance (Prop/Cas.)', 0.435656, 57),
    ('Investments & Asset Management', 0.529716, 283),
    ('Machinery', 0.867976, 105),
    ('Metals & Mining', 0.963643, 73),
    ('Office Equipment & Services', 0.980604, 14),
    ('Oil/Gas (Integrated)', 0.271219, 4),
    ('Oil/Gas (Production and Exploration)', 0.562760, 142),
    ('Oil/Gas Distribution', 0.465076, 23),
    ('Oilfield Svcs/Equip.', 0.743039, 97),
    ('Packaging & Container', 0.723069, 19),
    ('Paper/Forest Products', 0.721553, 6),
    ('Power', 0.310024, 46),
    ('Precious Metals', 0.793243, 56),
    ('Publishing & Newspapers', 0.477307, 19),
    ('R.E.I.T.', 0.392904, 190),
    ('Real Estate (Development)', 0.478288, 14),
    ('Real Estate (General/Diversified)', 0.577859, 12),
    ('Real Estate (Operations & Services)', 0.814892, 54),
    ('Recreation', 0.695091, 49),
    ('Reinsurance', 0.438409, 1),
    ('Restaurant/Dining', 0.767420, 64),
    ('Retail (Automotive)', 0.698433, 34),
    ('Retail (Building Supply)', 1.307014, 14),
    ('Retail (Distributors)', 0.782683, 62),
    ('Retail (General)', 0.759964, 23),
    ('Retail (Grocery and Food)', 0.804751, 15),
    ('Retail (REITs)', 0.436173, 26),
    ('Retail (Special Lines)', 0.948783, 94),
    ('Rubber& Tires', 0.143534, 3),
    ('Semiconductor', 1.489258, 66),
    ('Semiconductor Equip', 1.348073, 31),
    ('Shipbuilding & Marine', 0.644013, 8),
    ('Shoe', 0.933675, 11),
    ('Software (Entertainment)', 1.012781, 77),
    ('Software (Internet)', 1.546064, 29),
    ('Software (System & Application)', 1.225392, 309),
    ('Steel', 0.903514, 19),
    ('Telecom (Wireless)', 0.387259, 12),
    ('Telecom. Equipment', 0.863491, 57),
    ('Telecom. Services', 0.365434, 39),
    ('Tobacco', 0.677590, 10),
    ('Transportation', 0.675311, 19),
    ('Transportation (Railroads)', 0.806948, 4),
    ('Trucking', 0.850418, 26),
    ('Utility (General)', 0.148613, 14),
    ('Utility (Water)', 0.281177, 14),
    ('Total Market', 0.721730, 5994),
    ('Total Market (without financials)', 0.877517, 4822),
]

ACCENT = "#0F766E"   # teal: USD
FOREIGN = "#C2410C"  # burnt orange: foreign currency


# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap');
html, body, [class*="css"], .stApp, .stMarkdown, button, input, textarea, select {
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
}
#MainMenu, footer, [data-testid="stDecoration"] { visibility: hidden; height: 0; }
.block-container { padding-top: 3.6rem; padding-bottom: 3rem; max-width: 1280px; }

.hero .eyebrow { font-size: .78rem; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: #0F766E; margin-bottom: .35rem; }
.hero h1 { font-size: 2.15rem; font-weight: 700; letter-spacing: -.02em; line-height: 1.15;
    margin: 0; padding: 0; }
.hero p { opacity: .72; font-size: 1.02rem; margin-top: .5rem; max-width: 780px; }

.chips { display: flex; flex-wrap: wrap; gap: .5rem; margin: .9rem 0 .2rem 0; }
.chip { display: inline-flex; align-items: center; gap: .4rem; padding: .38rem .85rem;
    border-radius: 999px; border: 1px solid rgba(128,128,128,.28); font-size: .9rem;
    font-weight: 600; background: rgba(128,128,128,.05); }
.chip .k { font-weight: 500; opacity: .6; }

.kpi { border: 1px solid rgba(128,128,128,.22); border-radius: 16px; padding: 1rem 1.15rem;
    background: rgba(128,128,128,.04); height: 100%; }
.kpi .label { font-size: .76rem; font-weight: 500; opacity: .65; text-transform: uppercase;
    letter-spacing: .05em; }
.kpi .value { font-size: 1.7rem; font-weight: 700; letter-spacing: -.01em; margin-top: .25rem;
    font-variant-numeric: tabular-nums; }
.kpi .sub { font-size: .82rem; opacity: .65; margin-top: .15rem; }
.kpi.usd { border-top: 3px solid #0F766E; }
.kpi.fc  { border-top: 3px solid #C2410C; }

.cmp { width: 100%; border-collapse: separate; border-spacing: 0; font-size: .95rem;
    border: 1px solid rgba(128,128,128,.22); border-radius: 14px; overflow: hidden; }
.cmp th, .cmp td { padding: .6rem .9rem; text-align: right; font-variant-numeric: tabular-nums;
    border-bottom: 1px solid rgba(128,128,128,.14); }
.cmp th:first-child, .cmp td:first-child { text-align: left; }
.cmp thead th { font-size: .78rem; text-transform: uppercase; letter-spacing: .05em;
    font-weight: 600; background: rgba(128,128,128,.06); }
.cmp thead th.usd { color: #0F766E; } .cmp thead th.fc { color: #C2410C; }
.cmp td .note { display: block; font-size: .76rem; opacity: .55; font-weight: 400; }
.cmp tr.sec td { font-size: .74rem; text-transform: uppercase; letter-spacing: .06em;
    font-weight: 600; opacity: .6; background: rgba(128,128,128,.03); padding: .4rem .9rem; }
.cmp tr.total td { font-weight: 700; font-size: 1.05rem; border-bottom: none;
    background: rgba(15,118,110,.07); }
.cmp tr:last-child td { border-bottom: none; }

.src { border: 1px solid rgba(128,128,128,.22); border-radius: 14px; padding: .8rem 1rem;
    height: 100%; }
.src .t { font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; opacity: .6;
    font-weight: 600; }
.src .v { font-size: 1.15rem; font-weight: 700; margin: .2rem 0 .1rem 0;
    font-variant-numeric: tabular-nums; }
.src .d { font-size: .8rem; opacity: .7; }
.badge { display: inline-block; font-size: .68rem; font-weight: 700; letter-spacing: .04em;
    text-transform: uppercase; padding: .1rem .45rem; border-radius: 6px; margin-left: .35rem;
    vertical-align: middle; }
.badge.live { background: rgba(16,185,129,.15); color: #059669; }
.badge.manual { background: rgba(99,102,241,.15); color: #6366F1; }
.badge.fallback { background: rgba(245,158,11,.18); color: #B45309; }
.badge.missing { background: rgba(239,68,68,.15); color: #DC2626; }

.insight { border-left: 4px solid #0F766E; border-radius: 10px; padding: .85rem 1.1rem;
    background: rgba(15,118,110,.07); margin: 1rem 0 .6rem 0; font-size: .98rem; }
.step { font-size: .8rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
    opacity: .6; margin: 1.1rem 0 .3rem 0; }
[data-testid="stSidebar"] { border-right: 1px solid rgba(128,128,128,.15); }
.stButton > button, .stDownloadButton > button { border-radius: 10px; font-weight: 600; }
.stDownloadButton > button { background: #0F766E; color: white; border: none; }
.stDownloadButton > button:hover { background: #115E59; color: white; }
.stTabs [data-baseweb="tab"] { font-weight: 600; padding: .5rem 1rem; }
/* Recolor Streamlit's default red controls to the app's teal without a config file */
[data-testid="stSlider"], [data-testid="stButtonGroup"], [data-testid="stCheckbox"],
[data-testid="stToggle"], [role="tablist"] { filter: hue-rotate(172deg) saturate(.75); }
</style>
"""


# --------------------------------------------------------------------------
# HTTP + small helpers
# --------------------------------------------------------------------------
def http_get(url: str, **kwargs) -> requests.Response:
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    resp = requests.get(url, headers=headers, timeout=TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def to_float(text) -> float | None:
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def pct(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x * 100:.{nd}f}%"


# --------------------------------------------------------------------------
# Parsers (kept separate from fetching so they can be tested offline)
# --------------------------------------------------------------------------
def _excel_date(val) -> str | None:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    if isinstance(val, (pd.Timestamp, dt.datetime, dt.date)):
        return pd.Timestamp(val).strftime("%B %d, %Y")
    if isinstance(val, (int, float)) and 20000 < float(val) < 80000:  # Excel serial date
        return (pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(val))).strftime("%B %d, %Y")
    s = str(val).strip()
    return s or None


def parse_betas(content: bytes) -> tuple[pd.DataFrame, str | None]:
    """Parse Damodaran's betas.xls into Industry / Unlevered beta (+ extras)."""
    engine = "xlrd" if content[:4] == b"\xd0\xcf\x11\xe0" else None
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine=engine)
    for raw in sheets.values():
        hits = np.argwhere(
            raw.astype(str).apply(lambda c: c.str.strip().str.lower()).eq("industry name").values
        )
        if not len(hits):
            continue
        r, c = hits[0]
        header = [str(h).strip().lower() for h in raw.iloc[r].tolist()]

        def col(pred) -> int | None:
            return next((i for i, h in enumerate(header) if pred(h)), None)

        ub = col(lambda h: h == "unlevered beta") or col(
            lambda h: h.startswith("unlevered beta") and "cash" not in h
        )
        if ub is None:
            continue
        lb = col(lambda h: h == "beta")
        de = col(lambda h: h.startswith("d/e"))
        nf = col(lambda h: "number of firms" in h)
        body = raw.iloc[r + 1:]
        df = pd.DataFrame({
            "Industry": body.iloc[:, c].astype(str).str.strip(),
            "Unlevered beta": pd.to_numeric(body.iloc[:, ub], errors="coerce"),
            "Number of firms": pd.to_numeric(body.iloc[:, nf], errors="coerce") if nf is not None else np.nan,
            "Damodaran levered beta": pd.to_numeric(body.iloc[:, lb], errors="coerce") if lb is not None else np.nan,
            "Damodaran D/E": pd.to_numeric(body.iloc[:, de], errors="coerce") if de is not None else np.nan,
        })
        df = df[df["Industry"].ne("") & df["Industry"].str.lower().ne("nan") & df["Unlevered beta"].notna()]
        df = df.drop_duplicates("Industry").reset_index(drop=True)

        as_of = None
        for _, row in raw.iloc[:r].iterrows():
            cells = row.tolist()
            for i, v in enumerate(cells):
                if isinstance(v, str) and "date" in v.lower():
                    rest = [x for x in cells[i + 1:] if not (x is None or (isinstance(x, float) and np.isnan(x)))]
                    if rest:
                        as_of = _excel_date(rest[0])
                    break
            if as_of:
                break
        return df, as_of
    raise ValueError("Could not find an 'Industry Name' / 'Unlevered beta' table in betas.xls")


def parse_ratings(html: str) -> tuple[float, str | None]:
    """Return the Baa2/BBB spread for large non-financial firms (first table section)."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"as of\s+([A-Z][a-z]+\s+\d{4})", text) or re.search(
        r"Date of Analysis[:\s]+([A-Za-z]+\s+\d{4})", text)
    as_of = m.group(1) if m else None
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        for i, cell in enumerate(cells):
            if re.sub(r"\s", "", cell).upper() == "BAA2/BBB":
                for nxt in cells[i + 1:i + 3]:
                    v = to_float(nxt)
                    if v is not None:
                        return v / 100.0, as_of
    raise ValueError("Baa2/BBB row not found on the ratings page")


def parse_oecd_csv(text: str) -> dict[str, tuple[float, str]]:
    """OECD SDMX csv-with-labels -> {ISO3: (combined statutory CIT rate as decimal, year)}."""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    cols = {c.upper(): c for c in df.columns}
    obs, tp, area = cols.get("OBS_VALUE"), cols.get("TIME_PERIOD"), cols.get("REF_AREA")
    if not (obs and tp and area):
        raise ValueError("Unexpected OECD response layout")
    strcols = [c for c in df.columns if c not in (obs, tp)]
    low = df[strcols].fillna("").apply(lambda s: s.str.strip().str.lower())
    exact = low.eq("combined corporate income tax rate").any(axis=1)
    if exact.any():
        sel = df[exact]
    else:
        blob = low.agg(" | ".join, axis=1)
        sel = df[blob.str.contains("combined") & blob.str.contains("rate")
                 & ~blob.str.contains("targeted|small business|sub-central|central government")]
    sel = sel.assign(_v=pd.to_numeric(sel[obs], errors="coerce")).dropna(subset=["_v"])
    sel = sel.sort_values(tp)
    out: dict[str, tuple[float, str]] = {}
    for iso, g in sel.groupby(area):
        last = g.iloc[-1]
        v = float(last["_v"])
        out[str(iso)] = (v / 100.0 if v > 1 else v, str(last[tp]))
    if not out:
        raise ValueError("No combined statutory rates found in OECD response")
    return out


def _te_date(text: str, today: dt.date) -> str | None:
    s = (text or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        return today.isoformat()
    m = re.fullmatch(r"([A-Za-z]{3})/(\d{1,2})", s)
    if m:
        try:
            d = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
            if d > today + dt.timedelta(days=1):
                d = d.replace(year=today.year - 1)
            return d.isoformat()
        except ValueError:
            return None
    return None


def parse_te_bonds(html: str, today: dt.date | None = None) -> dict[str, tuple[float, str | None]]:
    """Trading Economics /bonds table -> {country name: (10Y yield decimal, date)}."""
    today = today or dt.date.today()
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, tuple[float, str | None]] = {}
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"/[a-z\-]+/government-bond-yield", re.I))
        if not a:
            continue
        slug = re.search(r"/([a-z\-]+)/government-bond-yield", a["href"], re.I).group(1).lower()
        label = a.get_text(" ", strip=True)
        tenor = re.search(r"\b(\d+)\s*([YM])\b", label.upper())
        if tenor and tenor.group(0).replace(" ", "") != "10Y":
            continue
        tds = tr.find_all("td")
        idx = next((i for i, td in enumerate(tds) if td.find("a") is a or a in td.descendants), -1)
        value = None
        for td in tds[idx + 1:]:
            value = to_float(td.get_text(strip=True))
            if value is not None:
                break
        date = None
        for td in reversed(tds):
            date = _te_date(td.get_text(strip=True), today)
            if date:
                break
        name = SLUG_TO_COUNTRY.get(slug, slug.replace("-", " ").title())
        if value is not None and name not in out:
            out[name] = (value / 100.0, date)
    if not out:
        raise ValueError("No bond rows found on the Trading Economics page")
    return out


def parse_te_country(html: str) -> tuple[float, str | None]:
    """Fallback: read the 10Y yield from a Trading Economics country page description."""
    soup = BeautifulSoup(html, "lxml")
    desc = ""
    for attrs in ({"name": "description"}, {"property": "og:description"}, {"id": "metaDesc"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = tag["content"]
            break
    if not desc:
        p = soup.find(id=re.compile("description", re.I)) or soup.find("h2")
        desc = p.get_text(" ", strip=True) if p else ""
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", desc)
    if not m:
        raise ValueError("No yield found in Trading Economics page description")
    d = re.search(r"([A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4})", desc)
    date = None
    if d:
        try:
            date = pd.Timestamp(re.sub(r"(st|nd|rd|th),?", ",", d.group(1))).date().isoformat()
        except Exception:  # noqa: BLE001
            date = d.group(1)
    return float(m.group(1)) / 100.0, date


# --------------------------------------------------------------------------
# Cached fetchers: each returns a dict {ok, data, as_of, error, fetched}
# --------------------------------------------------------------------------
@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_betas() -> dict:
    try:
        df, as_of = parse_betas(http_get(BETAS_URL).content)
        return {"ok": True, "status": "live", "data": df, "as_of": as_of, "error": None,
                "fetched": now_str()}
    except Exception as exc:  # noqa: BLE001
        df = pd.DataFrame(BETAS_SNAPSHOT, columns=["Industry", "Unlevered beta", "Number of firms"])
        return {"ok": True, "status": "fallback", "data": df,
                "as_of": f"{BETAS_SNAPSHOT_DATE} (built-in copy)", "error": str(exc), "fetched": now_str()}


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_bbb_spread() -> dict:
    try:
        spread, as_of = parse_ratings(http_get(RATINGS_URL).text)
        return {"ok": True, "data": spread, "as_of": as_of, "error": None, "fetched": now_str()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "data": None, "as_of": None, "error": str(exc), "fetched": now_str()}


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_tax_rates() -> dict:
    """Live OECD combined statutory rates; falls back to the 2024 snapshot per country."""
    live, err = {}, None
    try:
        resp = http_get(
            OECD_API_URL,
            params={"startPeriod": "2015", "dimensionAtObservation": "AllDimensions",
                    "format": "csvfilewithlabels"},
            headers={"Accept": "application/vnd.sdmx.data+csv; charset=utf-8; labels=both"},
        )
        live = parse_oecd_csv(resp.text)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    rates = {}
    for name, (iso, _, _, fb) in COUNTRIES.items():
        if iso in live:
            rates[name] = {"rate": live[iso][0], "as_of": live[iso][1], "status": "live"}
        else:
            rates[name] = {"rate": fb / 100.0, "as_of": "2024 (OECD Corporate Tax Statistics 2024, built-in)", "status": "fallback"}
    return {"ok": bool(live), "data": rates, "as_of": None, "error": err, "fetched": now_str()}


@st.cache_data(ttl=2 * 3600, show_spinner=False)
def load_bond_table() -> dict:
    try:
        table = parse_te_bonds(http_get(TE_BONDS_URL).text)
        return {"ok": True, "data": table, "as_of": None, "error": None, "fetched": now_str()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "data": {}, "as_of": None, "error": str(exc), "fetched": now_str()}


@st.cache_data(ttl=2 * 3600, show_spinner=False)
def load_country_yield(slug: str) -> dict:
    try:
        y, as_of = parse_te_country(http_get(TE_COUNTRY_URL.format(slug=slug)).text)
        return {"ok": True, "data": y, "as_of": as_of, "error": None, "fetched": now_str()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "data": None, "as_of": None, "error": str(exc), "fetched": now_str()}


def get_yield(country: str) -> dict:
    """10Y yield for a country: /bonds table first, then the country page."""
    table = load_bond_table()
    if country in table["data"]:
        y, d = table["data"][country]
        return {"value": y, "as_of": d or table["fetched"][:10], "status": "live",
                "url": TE_BONDS_URL, "error": None}
    single = load_country_yield(COUNTRIES[country][1])
    url = TE_COUNTRY_URL.format(slug=COUNTRIES[country][1])
    if single["ok"]:
        return {"value": single["data"], "as_of": single["as_of"] or single["fetched"][:10],
                "status": "live", "url": url, "error": None}
    return {"value": None, "as_of": None, "status": "missing", "url": url,
            "error": f"{table['error'] or 'not in /bonds table'}; {single['error']}"}


# --------------------------------------------------------------------------
# Finance
# --------------------------------------------------------------------------
def cost_of_capital(bu, t, d, erp, rf_us, rf_fc, s_us, s_fc) -> dict:
    """Works on scalars or numpy arrays of unlevered betas."""
    de = d / (1.0 - d) if d < 1 else np.inf
    bl = bu * (1.0 + (1.0 - t) * de)
    out = {"D/E": de, "levered_beta": bl}
    for ccy, rf, s in (("usd", rf_us, s_us), ("fc", rf_fc, s_fc)):
        ke = rf + bl * erp
        kd = rf + s
        kd_at = kd * (1.0 - t)
        out[ccy] = {"rf": rf, "ke": ke, "spread": s, "kd": kd, "kd_at": kd_at,
                    "wacc": (1.0 - d) * ke + d * kd_at}
    return out


def auto_foreign_spread(s_us: float, rf_us: float, rf_fc: float) -> float:
    """Spread that keeps the foreign cost of debt consistent with interest parity:
    (1 + rf_fc + s_fc) = (1 + rf_us + s_us) x (1 + rf_fc) / (1 + rf_us)."""
    return s_us * (1.0 + rf_fc) / (1.0 + rf_us)


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def badge(status: str) -> str:
    label = {"live": "Live", "manual": "Manual", "fallback": "Fallback", "missing": "Missing"}[status]
    return f'<span class="badge {status}">{label}</span>'


def kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="kpi {cls}"><div class="label">{label}</div>'
            f'<div class="value">{value}</div><div class="sub">{sub}</div></div>')


def src_card(title: str, value: str, date: str, status: str) -> str:
    return (f'<div class="src"><div class="t">{title}{badge(status)}</div>'
            f'<div class="v">{value}</div><div class="d">{date}</div></div>')


def override(label: str, key: str, auto: float | None, default_manual: float, scale: float,
             fmt: str, step: float, help_text: str, min_value: float | None = None) -> tuple[float | None, str]:
    """Toggle + number input. Returns (value as decimal/raw, status)."""
    on = st.toggle(f"Override {label}", key=f"tg_{key}", value=auto is None, help=help_text)
    if on:
        # If nothing was fetched, start empty so a placeholder is never used silently.
        start = None if auto is None else float(round(auto * scale, 4))
        if auto is None:
            st.caption(f"⚠️ Couldn't fetch the {label.split(' (')[0]}. Enter a value.")
        v = st.number_input(label, value=start, step=step, format=fmt, key=f"ni_{key}",
                            min_value=min_value, placeholder="Enter value",
                            label_visibility="collapsed")
        return (None if v is None else v / scale), "manual"
    return auto, ("live" if auto is not None else "missing")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="International Cost of Capital App", page_icon="🌐",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)

    with st.spinner("Fetching Damodaran, OECD and Trading Economics data…"):
        betas = load_betas()
        bbb = load_bbb_spread()
        taxes = load_tax_rates()

    # Industry list (Damodaran) — if the download fails, offer the default industry for manual beta.
    if betas["ok"]:
        ind_df = betas["data"]
        industries = ind_df["Industry"].tolist()
    else:
        ind_df = pd.DataFrame({"Industry": [DEFAULT_INDUSTRY], "Unlevered beta": [np.nan]})
        industries = [DEFAULT_INDUSTRY]
    countries = list(COUNTRIES.keys())

    # ---------------- Sidebar ----------------
    with st.sidebar:
        st.markdown("### 🌐 Setup")
        st.markdown('<div class="step">① Project</div>', unsafe_allow_html=True)
        industry = st.selectbox(
            "Industry (Damodaran, U.S.)", industries,
            index=industries.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in industries else 0,
            key="industry", help="Type to search. Betas are Damodaran's U.S. industry averages.")
        country = st.selectbox("Target country", countries, index=0, key="country",
                               help="Pick United States to compare the U.S. with itself.")

        st.markdown('<div class="step">② Financing assumptions</div>', unsafe_allow_html=True)
        erp_pct = st.slider("Equity risk premium (%)", 3.0, 12.0, DEFAULT_ERP, 0.1, key="erp",
                            help="Applied in both currencies.")
        debt_pct = st.slider("Target debt share, D/(D+E) (%)", 0, 80, DEFAULT_DEBT, 1, key="debt",
                             help="Applied in both currencies.")

        st.markdown('<div class="step">③ Manual overrides</div>', unsafe_allow_html=True)
        st.caption("Switch on any item to replace the fetched value with your own.")

    erp, d = erp_pct / 100.0, debt_pct / 100.0
    iso_fc, slug_fc, ccy_fc, _ = COUNTRIES[country]
    same = country == US

    # Auto (fetched) values
    row = ind_df.loc[ind_df["Industry"] == industry]
    bu_auto = float(row["Unlevered beta"].iloc[0]) if len(row) and pd.notna(row["Unlevered beta"].iloc[0]) else None
    t_us_auto = taxes["data"][US]["rate"]
    t_fc_auto = taxes["data"][country]["rate"]
    s_us_auto = bbb["data"] if bbb["ok"] else None
    with st.spinner("Fetching 10-year government bond yields…"):
        y_us = get_yield(US)
        y_fc = y_us if same else get_yield(country)

    with st.sidebar:
        with st.expander("Unlevered beta", expanded=bu_auto is None):
            bu, st_bu = override("unlevered beta", f"bu_{industry}", bu_auto, 1.0, 1.0, "%.3f", 0.01,
                                 "Damodaran's unlevered beta for the selected industry.", 0.0)
            if st_bu == "live" and betas["status"] == "fallback":
                st_bu = "fallback"
        with st.expander("Corporate tax rates"):
            t_us, st_tus = override("U.S. tax rate (%)", "tus", t_us_auto, 0.25, 100, "%.2f", 0.25,
                                    "OECD combined statutory rate.", 0.0)
            if same:
                t_fc, st_tfc = t_us, st_tus
                st.caption("Target country is the U.S., so both rates are the same.")
            else:
                t_fc, st_tfc = override(f"{country} tax rate (%)", f"tfc_{country}", t_fc_auto,
                                        0.25, 100, "%.2f", 0.25, "OECD combined statutory rate.", 0.0)
        with st.expander("BBB borrowing spreads"):
            s_us, st_sus = override("USD BBB spread (%)", "sus", s_us_auto, 0.0111, 100, "%.2f", 0.05,
                                    "Damodaran Baa2/BBB default spread, large non-financial firms.", 0.0)
        with st.expander("10-year government yields", expanded=y_us["value"] is None or y_fc["value"] is None):
            rf_us, st_rfus = override("U.S. 10Y yield (%)", "rfus", y_us["value"], 0.04, 100, "%.3f",
                                      0.05, "Trading Economics U.S. 10-year yield.")
            if same:
                rf_fc, st_rffc = rf_us, st_rfus
            else:
                rf_fc, st_rffc = override(f"{country} 10Y yield (%)", f"rffc_{country}", y_fc["value"],
                                          0.04, 100, "%.3f", 0.05,
                                          f"Trading Economics {country} 10-year yield.")

        # Foreign BBB spread: manual switch, otherwise the parity-based auto value.
        with st.expander(f"{ccy_fc} BBB spread", expanded=False):
            auto_ok = None not in (s_us, rf_us, rf_fc)
            s_fc_auto = auto_foreign_spread(s_us, rf_us, rf_fc) if auto_ok else None
            fc_on = st.toggle(f"Manual {ccy_fc} BBB spread", key=f"tg_sfc_{country}", value=False,
                              help="Off: USD spread × (1 + foreign yield) / (1 + U.S. yield).")
            if fc_on:
                s_fc = st.number_input(f"{ccy_fc} BBB spread (%)", key=f"ni_sfc_{country}",
                                       value=float(round((s_fc_auto or 0.0111) * 100, 4)),
                                       step=0.05, format="%.3f", min_value=0.0) / 100
                st_sfc = "manual"
            else:
                s_fc, st_sfc = s_fc_auto, "live" if auto_ok else "missing"
                st.number_input(f"{ccy_fc} BBB spread (%) · auto", disabled=True,
                                value=float(round((s_fc or 0) * 100, 4)), format="%.3f",
                                key=f"auto_sfc_{country}_{round((s_fc or 0) * 1e6)}")

        st.divider()
        if st.button("↻  Refresh data", width="stretch",
                     help="Clear the cache and fetch the latest data from all sources."):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Last fetch: {betas['fetched']}")

    # ---------------- Header ----------------
    st.markdown(
        f"""
        <div class="hero">
          <div class="eyebrow">Damodaran · OECD · Trading Economics</div>
          <h1>International Cost of Capital App</h1>
          <p>Estimate a project's cost of equity, cost of debt and WACC in U.S. dollars and in the
          target country's currency, using the same beta, tax rate, debt share and equity risk premium.</p>
        </div>
        <div class="chips">
          <span class="chip"><span class="k">Industry</span>{industry}</span>
          <span class="chip"><span class="k">Country</span>{country} · {ccy_fc}</span>
          <span class="chip"><span class="k">ERP</span>{erp_pct:.1f}%</span>
          <span class="chip"><span class="k">Debt</span>{debt_pct}%</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    checks = [("unlevered beta", bu), ("U.S. tax rate", t_us), ("USD BBB spread", s_us),
              ("U.S. 10-year yield", rf_us)]
    if not same:
        checks += [(f"{country} tax rate", t_fc), (f"{country} 10-year yield", rf_fc)]
    missing = [n for n, v in checks if v is None]
    if missing:
        st.error("Couldn't fetch: **" + ", ".join(missing) + "**. Enter the value under "
                 "**③ Manual overrides** in the sidebar, or click **Refresh data** to try again.",
                 icon="🔌")
        errs = {"Damodaran betas": betas["error"], "Damodaran ratings": bbb["error"],
                "OECD": taxes["error"], "Trading Economics (U.S.)": y_us["error"],
                f"Trading Economics ({country})": None if same else y_fc["error"]}
        with st.expander("Technical details"):
            for k, v in errs.items():
                if v:
                    st.code(f"{k}: {v}", language=None)
        st.stop()

    # ---------------- Tax switch + results ----------------
    tax_options = ["U.S. tax rate"] if same else ["U.S. tax rate", f"{country} tax rate"]
    tax_choice = st.segmented_control(
        "Project tax rate", tax_options, default=tax_options[0], key=f"taxsel_{country}",
        help="The chosen rate is used in both currencies (beta relevering and the debt tax shield).",
    ) or tax_options[0]
    use_fc_tax = tax_choice != "U.S. tax rate"
    t = t_fc if use_fc_tax else t_us
    t_src = country if use_fc_tax else US

    res = cost_of_capital(bu, t, d, erp, rf_us, rf_fc, s_us, s_fc)
    usd, fc = res["usd"], res["fc"]
    parity_wacc = (1 + usd["wacc"]) * (1 + rf_fc) / (1 + rf_us) - 1

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(kpi("WACC · USD", pct(usd["wacc"]), f"U.S. 10Y {pct(rf_us)}", "usd"), unsafe_allow_html=True)
    k2.markdown(kpi(f"WACC · {ccy_fc}", pct(fc["wacc"]), f"{country} 10Y {pct(rf_fc)}", "fc"),
                unsafe_allow_html=True)
    k3.markdown(kpi("Levered beta", f"{res['levered_beta']:.3f}", f"Unlevered {bu:.3f} · D/E {res['D/E']:.2f}"),
                unsafe_allow_html=True)
    k4.markdown(kpi("Project tax rate", pct(t), f"{t_src} rate"), unsafe_allow_html=True)

    gap = fc["wacc"] - usd["wacc"]
    if same:
        msg = (f"For a <b>{industry}</b> project in the United States, the WACC is "
               f"<b>{pct(usd['wacc'])}</b> in USD, with a cost of equity of {pct(usd['ke'])}.")
    else:
        msg = (f"For a <b>{industry}</b> project, the WACC is <b>{pct(usd['wacc'])}</b> in USD and "
               f"<b>{pct(fc['wacc'])}</b> in {ccy_fc}, a gap of <b>{gap * 1e4:+.0f} bp</b>, driven by the "
               f"{(rf_fc - rf_us) * 1e4:+.0f} bp difference in 10-year yields.")
    st.markdown(f'<div class="insight">💡 {msg}</div>', unsafe_allow_html=True)

    tab_res, tab_all, tab_method, tab_src = st.tabs(
        ["Results", "All industries", "Assumptions & method", "Data sources"])

    # ---------------- Results tab ----------------
    fc_head = f"{country} · {ccy_fc}"
    rows = [
        ("sec", "Cost of equity", None, None, ""),
        ("", "Risk-free rate (10Y government yield)", rf_us, rf_fc, "Trading Economics"),
        ("", "Levered beta", res["levered_beta"], res["levered_beta"], "Hamada, project tax rate"),
        ("", "Equity risk premium", erp, erp, "User input"),
        ("", "Cost of equity", usd["ke"], fc["ke"], "rf + β × ERP"),
        ("sec", "Cost of debt", None, None, ""),
        ("", "BBB default spread", s_us, s_fc,
         "Damodaran (USD) · " + ("manual" if st_sfc == "manual" else "parity-adjusted")),
        ("", "Pre-tax cost of debt", usd["kd"], fc["kd"], "rf + spread"),
        ("", "Project tax rate", t, t, f"OECD, {t_src}"),
        ("", "After-tax cost of debt", usd["kd_at"], fc["kd_at"], "× (1 − t)"),
        ("sec", "Weights", None, None, ""),
        ("", "Equity share, E/V", 1 - d, 1 - d, "User input"),
        ("", "Debt share, D/V", d, d, "User input"),
        ("total", "WACC", usd["wacc"], fc["wacc"], ""),
    ]

    def cell(v, label):
        if v is None:
            return ""
        return f"{v:.3f}" if label == "Levered beta" else pct(v)

    body = "".join(
        f'<tr class="sec"><td colspan="3">{lab}</td></tr>' if kind == "sec" else
        f'<tr class="{kind}"><td>{lab}<span class="note">{note}</span></td>'
        f'<td>{cell(a, lab)}</td><td>{cell(b, lab)}</td></tr>'
        for kind, lab, a, b, note in rows
    )
    with tab_res:
        left, right = st.columns([1.15, 1])
        with left:
            st.markdown(
                f'<table class="cmp"><thead><tr><th></th><th class="usd">United States · USD</th>'
                f'<th class="fc">{fc_head}</th></tr></thead><tbody>{body}</tbody></table>',
                unsafe_allow_html=True)
            if not same:
                st.caption(f"Parity check: USD WACC converted with the yield ratio "
                           f"(1 + WACC_USD) × (1 + rf_{ccy_fc}) / (1 + rf_USD) − 1 = **{pct(parity_wacc)}** "
                           f"vs. {pct(fc['wacc'])} computed directly.")
        with right:
            cats = ["Cost of<br>equity", "Pre-tax<br>cost of debt", "After-tax<br>cost of debt", "<b>WACC</b>"]
            fig = go.Figure()
            fig.add_bar(name="USD", x=cats, y=[usd["ke"] * 100, usd["kd"] * 100, usd["kd_at"] * 100,
                                                 usd["wacc"] * 100],
                        marker_color=ACCENT, text=[pct(v) for v in (usd["ke"], usd["kd"], usd["kd_at"], usd["wacc"])],
                        textposition="outside", cliponaxis=False)
            if not same:
                fig.add_bar(name=ccy_fc, x=cats, y=[fc["ke"] * 100, fc["kd"] * 100, fc["kd_at"] * 100,
                                                     fc["wacc"] * 100],
                            marker_color=FOREIGN, text=[pct(v) for v in (fc["ke"], fc["kd"], fc["kd_at"], fc["wacc"])],
                            textposition="outside", cliponaxis=False)
            fig.update_layout(
                barmode="group", height=430, margin=dict(l=8, r=8, t=40, b=8),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Inter, system-ui, sans-serif", size=12),
                legend=dict(orientation="h", y=1.1, x=0), bargap=0.3,
                yaxis=dict(title="%", gridcolor="rgba(128,128,128,.15)", zeroline=False),
                xaxis=dict(tickangle=0),
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    # ---------------- All industries ----------------
    all_df = ind_df.dropna(subset=["Unlevered beta"]).copy() if betas["ok"] else pd.DataFrame()
    if not all_df.empty:
        r_all = cost_of_capital(all_df["Unlevered beta"].to_numpy(), t, d, erp, rf_us, rf_fc, s_us, s_fc)
        all_df["Levered beta"] = r_all["levered_beta"]
        all_df["Cost of equity USD (%)"] = r_all["usd"]["ke"] * 100
        all_df[f"Cost of equity {ccy_fc} (%)"] = r_all["fc"]["ke"] * 100
        all_df["WACC USD (%)"] = r_all["usd"]["wacc"] * 100
        all_df[f"WACC {ccy_fc} (%)"] = r_all["fc"]["wacc"] * 100
        all_df = all_df.drop(columns=["Damodaran levered beta", "Damodaran D/E"], errors="ignore")
        all_df = all_df.sort_values("WACC USD (%)", ascending=False).reset_index(drop=True)
        all_df.insert(0, "Rank", range(1, len(all_df) + 1))

    with tab_all:
        if all_df.empty:
            st.info("The all-industry comparison needs Damodaran's beta file. Click **Refresh data** to retry.")
        else:
            sel_rank = int(all_df.loc[all_df["Industry"] == industry, "Rank"].iloc[0]) \
                if industry in set(all_df["Industry"]) else None
            st.markdown(
                f"**{len(all_df)} industries** under the same assumptions (tax {pct(t)}, debt {debt_pct}%, "
                f"ERP {erp_pct:.1f}%)." + (f" **{industry}** ranks **#{sel_rank}** by USD WACC." if sel_rank else ""))
            c1, c2 = st.columns([2, 1])
            q = c1.text_input("Filter industries", placeholder="e.g. software, oil, bank", key="ind_filter")
            scope = c2.segmented_control("Chart", ["Top & bottom 15", "All"], default="Top & bottom 15",
                                         key="chart_scope") or "Top & bottom 15"
            view = all_df[all_df["Industry"].str.contains(q, case=False, regex=False)] if q else all_df
            fc_col = f"WACC {ccy_fc} (%)"
            if scope == "All" or len(view) <= 30:
                chart_df = view
            else:
                chart_df = pd.concat([view.head(15), view.tail(15)])
                if industry in set(view["Industry"]) and industry not in set(chart_df["Industry"]):
                    chart_df = pd.concat([chart_df, view[view["Industry"] == industry]])
                chart_df = chart_df.sort_values("WACC USD (%)", ascending=False)
            chart_df = chart_df.iloc[::-1]
            colors = ["#0F766E" if i != industry else "#6366F1" for i in chart_df["Industry"]]
            fig2 = go.Figure()
            fig2.add_bar(y=chart_df["Industry"], x=chart_df["WACC USD (%)"], orientation="h", name="USD",
                         marker_color=colors, hovertemplate="%{y}<br>USD WACC %{x:.2f}%<extra></extra>")
            if not same:
                fig2.add_scatter(y=chart_df["Industry"], x=chart_df[fc_col], mode="markers", name=ccy_fc,
                                 marker=dict(color=FOREIGN, size=9, symbol="diamond"),
                                 hovertemplate=f"%{{y}}<br>{ccy_fc} WACC %{{x:.2f}}%<extra></extra>")
            fig2.update_layout(
                height=max(380, 22 * len(chart_df) + 80), margin=dict(l=8, r=8, t=30, b=8),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Inter, system-ui, sans-serif", size=12),
                legend=dict(orientation="h", y=1.02, x=0), showlegend=not same,
                xaxis=dict(title="WACC (%)", gridcolor="rgba(128,128,128,.15)"),
                yaxis=dict(automargin=True),
            )
            st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})
            st.caption(f"{industry} is highlighted in indigo. Bars show USD WACC"
                       + ("" if same else f"; diamonds show {ccy_fc} WACC") + ".")
            st.dataframe(
                view, width="stretch", hide_index=True, height=420,
                column_config={
                    "Unlevered beta": st.column_config.NumberColumn(format="%.3f"),
                    "Levered beta": st.column_config.NumberColumn(format="%.3f"),
                    "Number of firms": st.column_config.NumberColumn(format="%d"),
                    **{c: st.column_config.NumberColumn(format="%.2f") for c in view.columns if "(%)" in c},
                },
            )

    # ---------------- Method ----------------
    with tab_method:
        st.markdown("#### Formulas")
        m1, m2 = st.columns(2)
        with m1:
            st.markdown("**Relever the industry beta** at the target debt share and project tax rate:")
            st.latex(r"\beta_L = \beta_U\left[1 + (1-t)\,\frac{D}{E}\right],\qquad \frac{D}{E}=\frac{d}{1-d}")
            st.markdown("**Cost of equity (CAPM)** in each currency, with that currency's 10-year yield:")
            st.latex(r"k_e = r_f + \beta_L \times ERP")
            st.markdown("**Foreign BBB spread (auto)** keeps the foreign cost of debt consistent "
                        "with interest-rate parity:")
            st.latex(r"s_{FC} = s_{USD}\times\frac{1+r_{f,FC}}{1+r_{f,US}}")
        with m2:
            st.markdown("**Cost of debt**:")
            st.latex(r"k_d = r_f + s,\qquad k_d^{\,after\text{-}tax} = k_d\,(1-t)")
            st.markdown("**WACC**:")
            st.latex(r"WACC = (1-d)\,k_e + d\,k_d\,(1-t)")
            st.markdown("**Parity check** (shown under the results table):")
            st.latex(r"WACC_{FC}^{parity} = (1+WACC_{USD})\,\frac{1+r_{f,FC}}{1+r_{f,US}} - 1")
        st.markdown("#### Main assumptions")
        st.markdown(
            f"""
- **Same project, two currencies.** The beta, project tax rate ({pct(t)}, {t_src}), debt share
  ({debt_pct}%) and equity risk premium ({erp_pct:.1f}%) are identical in both columns. Only the
  risk-free rate and the BBB spread change with the currency.
- **Beta.** Damodaran's U.S. industry unlevered beta is assumed to describe the project's business
  risk in any country. It is relevered with the Hamada formula, so debt is assumed to be risk-free
  for beta purposes and the tax shield is valued at the project tax rate.
- **Risk-free rates.** Local-currency 10-year government yields from Trading Economics. For countries
  with sovereign default risk, the local yield includes a default premium, so the foreign-currency
  cost of capital also embeds country risk; no separate country risk premium is added.
- **Equity risk premium.** One mature-market ERP is used in both currencies.
- **Cost of debt.** The project borrows at a BBB (Baa2) rating. The USD spread is Damodaran's default
  spread for large non-financial firms. The foreign spread is scaled by the yield ratio unless you
  enter it manually.
- **Tax rate.** OECD combined (central plus sub-central) statutory corporate income tax rate, latest
  year available. The switch above the results chooses the U.S. or the target country's rate.
- **Weights.** Target market-value weights, held constant over the project's life.
"""
        )

    # ---------------- Sources ----------------
    tax_live_us = taxes["data"][US]
    tax_live_fc = taxes["data"][country]
    with tab_src:
        st.markdown("#### Values used and where they came from")
        s1, s2, s3, s4 = st.columns(4)
        s1.markdown(src_card("Unlevered beta", f"{bu:.3f}", f"Damodaran · {betas['as_of'] or 'date n/a'}", st_bu),
                    unsafe_allow_html=True)
        s2.markdown(src_card("USD BBB spread", pct(s_us), f"Damodaran · as of {bbb['as_of'] or 'n/a'}", st_sus),
                    unsafe_allow_html=True)
        if "manual" in (st_tus, st_tfc):
            tax_status = "manual"
        elif "fallback" in (tax_live_us["status"], tax_live_fc["status"]):
            tax_status = "fallback"
        else:
            tax_status = "live"
        yld_status = "manual" if "manual" in (st_rfus, st_rffc) else "live"
        s3.markdown(src_card("Tax rates · U.S. / " + ("U.S." if same else country), f"{pct(t_us)} / {pct(t_fc)}",
                             f"OECD · {tax_live_us['as_of']}", tax_status), unsafe_allow_html=True)
        s4.markdown(src_card("10Y yields · U.S. / " + ("U.S." if same else country), f"{pct(rf_us)} / {pct(rf_fc)}",
                             f"Trading Economics · {y_us['as_of'] or 'n/a'}", yld_status),
                    unsafe_allow_html=True)
        st.write("")
        src_tbl = pd.DataFrame([
            ["Unlevered beta", industry, f"{bu:.3f}", "Damodaran, U.S. industry betas (betas.xls)",
             betas["as_of"] or "n/a", st_bu.title(), BETAS_URL],
            ["BBB spread (USD)", "Baa2/BBB, large non-financial firms", pct(s_us),
             "Damodaran, ratings & default spreads", bbb["as_of"] or "n/a", st_sus.title(), RATINGS_URL],
            [f"BBB spread ({ccy_fc})", "Parity-adjusted" if st_sfc != "manual" else "Manual", pct(s_fc),
             "Calculated from USD spread and 10Y yields" if st_sfc != "manual" else "User input",
             "n/a", st_sfc.title(), ""],
            ["Corporate tax rate", US, pct(t_us), "OECD Corporate Tax Statistics (combined statutory)",
             tax_live_us["as_of"], ("Manual" if st_tus == "manual" else tax_live_us["status"].title()),
             OECD_API_URL if tax_live_us["status"] == "live" else OECD_REPORT_URL],
            ["Corporate tax rate", country, pct(t_fc), "OECD Corporate Tax Statistics (combined statutory)",
             tax_live_fc["as_of"], ("Manual" if st_tfc == "manual" else tax_live_fc["status"].title()),
             OECD_API_URL if tax_live_fc["status"] == "live" else OECD_REPORT_URL],
            ["10Y government yield", US, pct(rf_us, 3), "Trading Economics", y_us["as_of"] or "n/a",
             st_rfus.title(), y_us["url"]],
            ["10Y government yield", country, pct(rf_fc, 3), "Trading Economics", y_fc["as_of"] or "n/a",
             st_rffc.title(), y_fc["url"]],
        ], columns=["Input", "Detail", "Value", "Source", "As of", "Status", "Link"])
        st.dataframe(src_tbl, width="stretch", hide_index=True,
                     column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open")})
        st.caption(
            "**Live** = fetched this session · **Fallback** = live request failed, built-in copy of the source used · "
            f"**Manual** = your override. Data last fetched {betas['fetched']}. Use **Refresh data** in the "
            "sidebar to fetch again.")
        if betas["status"] == "fallback":
            st.warning(f"Damodaran's site didn't respond, so betas come from the built-in copy of betas.xls "
                       f"(updated {BETAS_SNAPSHOT_DATE}).", icon="⚠️")
        if not taxes["ok"]:
            st.warning("The OECD data service didn't respond, so tax rates use the 2024 combined statutory "
                       "rates from OECD Corporate Tax Statistics 2024 (Figure 2.1).", icon="⚠️")

    # ---------------- Downloads ----------------
    summary = pd.DataFrame(
        [[lab, (a * 100 if lab != "Levered beta" else a), (b * 100 if lab != "Levered beta" else b), note]
         for kind, lab, a, b, note in rows if kind != "sec"],
        columns=["Item", "USD", ccy_fc, "Note"])
    summary["Unit"] = np.where(summary["Item"].eq("Levered beta"), "x", "%")
    inputs = pd.DataFrame([
        ["Industry", industry], ["Target country", country], ["Foreign currency", ccy_fc],
        ["Unlevered beta", bu], ["Equity risk premium (%)", erp_pct], ["Debt share (%)", debt_pct],
        ["Project tax rate source", t_src], ["Project tax rate (%)", t * 100],
        ["U.S. tax rate (%)", t_us * 100], [f"{country} tax rate (%)", t_fc * 100],
        ["USD BBB spread (%)", s_us * 100], [f"{ccy_fc} BBB spread (%)", s_fc * 100],
        ["U.S. 10Y yield (%)", rf_us * 100], [f"{country} 10Y yield (%)", rf_fc * 100],
        ["Generated", now_str()],
    ], columns=["Input", "Value"])

    st.markdown("#### Download")
    d1, d2, d3 = st.columns(3)
    csv_res = io.StringIO()
    inputs.to_csv(csv_res, index=False)
    csv_res.write("\n")
    summary.to_csv(csv_res, index=False)
    tag = f"{re.sub(r'[^A-Za-z0-9]+', '_', industry).strip('_')}_{COUNTRIES[country][0]}"
    d1.download_button("⬇  Results (CSV)", csv_res.getvalue().encode("utf-8"),
                       file_name=f"cost_of_capital_{tag}.csv", mime="text/csv", width="stretch")
    if not all_df.empty:
        d2.download_button("⬇  All industries (CSV)", all_df.to_csv(index=False).encode("utf-8"),
                           file_name=f"all_industries_{COUNTRIES[country][0]}.csv", mime="text/csv",
                           width="stretch")
    xbuf = io.BytesIO()
    with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
        inputs.to_excel(xw, sheet_name="Inputs", index=False)
        summary.to_excel(xw, sheet_name="Results", index=False)
        src_tbl.to_excel(xw, sheet_name="Sources", index=False)
        if not all_df.empty:
            all_df.to_excel(xw, sheet_name="All industries", index=False)
    d3.download_button("⬇  Everything (Excel)", xbuf.getvalue(), file_name=f"cost_of_capital_{tag}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")


if __name__ == "__main__":
    main()
