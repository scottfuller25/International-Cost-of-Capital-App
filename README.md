# International Cost of Capital App

A Streamlit app that estimates a project's **cost of equity, cost of debt and WACC in U.S. dollars and in a target country's currency**, side by side. It uses live data from Aswath Damodaran, the OECD and Trading Economics.

## Features

- **Live data, with sources and dates.** Industry betas and the BBB spread come from Damodaran, corporate tax rates from the OECD, and 10-year government yields from Trading Economics. Each value shows its source, as-of date and status (Live / Fallback / Manual).
- **Refresh data** button that clears the cache and fetches everything again.
- **Selectors** for industry and target country; **sliders** for the equity risk premium and target debt share.
- **Manual overrides** for the unlevered beta, the U.S. and foreign tax rates, the USD BBB spread, and both 10-year yields.
- **Foreign-currency BBB spread switch.** Off shows the automatic parity-based calculation; on lets you type your own value.
- **Project tax rate switch.** Use the U.S. or the target country's rate. The chosen rate applies in both currencies.
- **Side-by-side results table and chart**: USD vs. foreign currency.
- **All-industry comparison** of every Damodaran industry under the same assumptions, with a filter and a ranked chart.
- **Downloads**: results CSV, all-industries CSV, and an Excel workbook with Inputs, Results, Sources and All industries sheets.

### Defaults when the app opens

| Setting | Default |
|---|---|
| Industry | Software (System & Application) |
| Target country | United States |
| Project tax rate | U.S. |
| Equity risk premium | 6.5% |
| Debt share, D/(D+E) | 30% |

## Method

The same unlevered beta, project tax rate *t*, debt share *d* and equity risk premium (ERP) are used in both currencies. Only the risk-free rate and the BBB spread differ.

| Step | Formula |
|---|---|
| Debt-to-equity | D/E = d / (1 − d) |
| Levered beta (Hamada) | βL = βU × [1 + (1 − t) × D/E] |
| Cost of equity (CAPM) | ke = rf + βL × ERP |
| Foreign BBB spread (auto) | s_FC = s_USD × (1 + rf_FC) / (1 + rf_US) |
| Pre-tax cost of debt | kd = rf + s |
| After-tax cost of debt | kd × (1 − t) |
| WACC | (1 − d) × ke + d × kd × (1 − t) |

In each column, rf is that currency's 10-year government yield. The automatic foreign spread keeps the foreign cost of debt consistent with interest-rate parity: (1 + kd_FC) = (1 + kd_USD) × (1 + rf_FC) / (1 + rf_US). As a cross-check, the app also shows the USD WACC converted with the same yield ratio.

### Main assumptions

- Damodaran's U.S. industry unlevered beta represents the project's business risk in any country.
- Debt carries no beta (Hamada), and the tax shield uses the project tax rate.
- One mature-market ERP applies in both currencies. Local 10-year yields already include any sovereign default premium, so no separate country risk premium is added.
- The project borrows at a BBB (Baa2) rating.
- Weights are constant target market-value weights.

## Data sources

| Input | Source | What the app reads |
|---|---|---|
| Unlevered beta | [Damodaran, betas.xls](https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls) | "Unlevered beta" column for each U.S. industry, plus the file's "Date updated" |
| USD BBB spread | [Damodaran, ratings.html](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html) | Baa2/BBB spread for large non-financial service firms, plus the "Data used is as of" date |
| Corporate tax rates | [OECD Corporate Tax Statistics](https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/full-report/statutory-corporate-income-tax-rates_440f9286.html) | Combined (central + sub-central) statutory CIT rate, latest year, from the OECD Data Explorer API (`OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT`) |
| 10-year government yields | [Trading Economics bonds](https://tradingeconomics.com/bonds) | 10Y yield and date from the bonds table. If a country isn't in the table, the app reads that country's `/government-bond-yield` page |

### If a source is unavailable

The app tries the live source first. If it fails, it uses a built-in copy and labels those values **Fallback**:

- **Damodaran betas:** The app contains a copy of all 96 industries from `betas.xls` ("Industry Averages" sheet, updated January 5, 2026). For example, Software (System & Application) has an unlevered beta of 1.2254, from 309 firms.
- **OECD tax rates:** The app contains the 2024 combined statutory rates from *Corporate Tax Statistics 2024*, Figure 2.1 (e.g. United States 25.63%, Japan 29.74%, Germany 29.93%, India 25.17%). Taiwan isn't covered by the OECD report, so it uses Taiwan's general 20% rate.
- **Damodaran BBB spread and Trading Economics yields:** These change often, so there's no built-in copy. If either can't be reached, the app says which value is missing and asks you to enter it under **③ Manual overrides**. It never substitutes a made-up number.
- **Trading Economics scraping:** Trading Economics may block automated requests from some servers. If yields show as missing, click **Refresh data** or enter them manually.

## Run locally (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push `app.py`, `requirements.txt` and `README.md` to the root of a GitHub repository.
2. At [share.streamlit.io](https://share.streamlit.io), click **Create app**, choose the repository, and set the main file to `app.py`.
3. Under **Advanced settings**, choose Python 3.12, then click **Deploy**.

## Files

- `app.py`: the Streamlit application
- `requirements.txt`: Python dependencies
- `README.md`: this file
