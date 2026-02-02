from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import requests
from loguru import logger


def load_config() -> dict:
    config_path = Path(__file__).with_name("config.json")
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if "globalM2Btc" not in config:
        raise KeyError("Missing 'globalM2Btc' in config.json.")
    return config["globalM2Btc"]


def configure_logging(log_path: Path, rotation: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(log_path, level="INFO", rotation=rotation)


def normalize_month_start(series: pd.Series) -> pd.Series:
    series = series.copy()
    series.index = series.index.to_period("M").to_timestamp(how="start")
    return series


def monthly_last(series: pd.Series) -> pd.Series:
    return series.resample("MS").last().dropna()


def fetch_btc_prices() -> pd.Series:
    url = "https://api.blockchain.info/charts/market-price?timespan=all&format=csv"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text))
    if len(df.columns) != 2:
        raise ValueError("Unexpected Blockchain.com response for BTC price data.")
    df.columns = ["date", "price"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    series = df.dropna(subset=["date", "price"]).set_index("date")["price"].sort_index()
    if series.empty:
        raise ValueError("Blockchain.com returned empty BTC price series.")
    return series


def parse_macromicro_series(payload: object) -> pd.Series:
    data: object = payload
    if isinstance(payload, dict):
        if "data" in payload:
            data = payload["data"]
            if isinstance(data, dict):
                if "data" in data:
                    data = data["data"]
                elif len(data) == 1:
                    container = next(iter(data.values()))
                    if isinstance(container, dict) and "series" in container:
                        series_block = container["series"]
                        if isinstance(series_block, list) and series_block:
                            first = series_block[0]
                            if (
                                isinstance(first, list)
                                and first
                                and isinstance(first[0], (list, tuple))
                            ):
                                data = first
                            else:
                                data = series_block
        elif "chartData" in payload:
            data = payload["chartData"]
        else:
            raise ValueError("Unexpected MacroMicro payload shape.")

    if not isinstance(data, list) or not data:
        raise ValueError("MacroMicro payload contains no data points.")

    first = data[0]
    if isinstance(first, dict):
        date_key = next(
            (key for key in ("date", "t", "x") if key in first),
            None,
        )
        value_key = next(
            (key for key in ("value", "v", "y") if key in first),
            None,
        )
        if date_key is None or value_key is None:
            raise ValueError("MacroMicro data points missing date/value keys.")
        df = pd.DataFrame(data)
        df["date"] = coerce_macromicro_dates(df[date_key])
        df["value"] = pd.to_numeric(df[value_key], errors="coerce")
    elif isinstance(first, (list, tuple)) and len(first) >= 2:
        df = pd.DataFrame(data, columns=["date", "value"])
        df["date"] = coerce_macromicro_dates(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    else:
        raise ValueError("MacroMicro data points have an unsupported format.")

    df["date"] = df["date"].dt.tz_convert(None)
    series = df.dropna(subset=["date", "value"]).set_index("date")["value"].sort_index()
    if series.empty:
        raise ValueError("MacroMicro returned empty data series.")
    return series


def coerce_macromicro_dates(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        numeric = values.dropna()
        if not numeric.empty:
            max_val = numeric.max()
            if max_val > 10**12:
                return pd.to_datetime(values, unit="ms", errors="coerce", utc=True)
            if max_val > 10**10:
                return pd.to_datetime(values, unit="s", errors="coerce", utc=True)
            if max_val > 10**7:
                return pd.to_datetime(values.astype(str), errors="coerce", utc=True)
    return pd.to_datetime(values, errors="coerce", utc=True)


def extract_macromicro_unit(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in ("unit", "chart_unit", "unitName", "unit_name"):
            if key in payload and payload[key]:
                return str(payload[key])
        if "data" in payload and isinstance(payload["data"], dict):
            if len(payload["data"]) == 1:
                container = next(iter(payload["data"].values()))
                if isinstance(container, dict):
                    info = container.get("info")
                    if isinstance(info, dict):
                        for key in ("unit", "unitName", "unit_name"):
                            if info.get(key):
                                return str(info[key])
            for key in ("unit", "unitName", "unit_name"):
                if key in payload["data"] and payload["data"][key]:
                    return str(payload["data"][key])
    return None


def fetch_global_m2_macromicro(config: dict) -> tuple[pd.Series, str | None]:
    token = os.environ.get("MACROMICRO_BEARER")
    cookie = os.environ.get("MACROMICRO_COOKIE")
    if not token:
        raise ValueError("MACROMICRO_BEARER is not set in the environment.")
    if not cookie:
        raise ValueError("MACROMICRO_COOKIE is not set in the environment.")

    if shutil.which("curl") is None:
        raise ValueError("curl is required to fetch MacroMicro data.")

    headers = [
        "accept: */*",
        f"authorization: Bearer {token}",
        f"referer: {config['referer']}",
    ]
    if "acceptLanguage" in config:
        headers.append(f"accept-language: {config['acceptLanguage']}")
    if "userAgent" in config:
        headers.append(f"user-agent: {config['userAgent']}")

    extra_headers = config.get("headers", {})
    if extra_headers:
        forbidden = {"authorization", "cookie"}
        forbidden_present = forbidden.intersection(
            {key.lower() for key in extra_headers}
        )
        if forbidden_present:
            raise ValueError(
                "MacroMicro headers in config cannot include authorization or cookie."
            )
        for key, value in extra_headers.items():
            if value == "":
                headers.append(f"{key}:")
            else:
                headers.append(f"{key}: {value}")

    args = ["curl", "-sS", config["url"]]
    for header in headers:
        args.extend(["-H", header])
    args.extend(["-b", cookie])

    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ValueError(f"curl failed fetching MacroMicro data: {stderr}")
    if not result.stdout:
        raise ValueError("curl returned empty MacroMicro response.")
    payload = json.loads(result.stdout)
    series = parse_macromicro_series(payload)
    unit = extract_macromicro_unit(payload)
    return series, unit


def apply_lead_months(series: pd.Series, lead_months: int) -> pd.Series:
    shifted = series.copy()
    shifted.index = shifted.index + pd.DateOffset(months=lead_months)
    return shifted


def align_series(global_m2: pd.Series, btc_usd: pd.Series) -> pd.DataFrame:
    combined = pd.concat(
        {"global_m2": global_m2, "btc_usd": btc_usd},
        axis=1,
        join="inner",
    ).dropna()
    if combined.empty:
        raise ValueError("No overlapping dates after alignment.")
    return combined


def add_rolling_correlation(
    combined: pd.DataFrame, window_months: int
) -> pd.DataFrame:
    if window_months < 2:
        raise ValueError("correlationWindowMonths must be >= 2.")
    combined = combined.copy()
    combined["rolling_corr"] = (
        combined["global_m2"].rolling(window=window_months).corr(combined["btc_usd"])
    )
    return combined


def focus_last_years(combined: pd.DataFrame, years: int) -> pd.DataFrame:
    if years < 1:
        raise ValueError("focusYears must be >= 1.")
    last_date = combined.index.max()
    start_date = last_date - pd.DateOffset(years=years)
    return combined.loc[combined.index >= start_date].copy()


def plot_series(
    combined: pd.DataFrame,
    output_path: Path,
    title: str,
    m2_label: str,
    m2_axis_label: str,
    corr_label: str,
) -> None:
    fig, (ax_btc, ax_corr) = plt.subplots(
        2,
        1,
        figsize=(12, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax_btc.plot(combined.index, combined["btc_usd"], color="tab:blue", label="BTC")
    ax_btc.set_ylabel("BTC price (USD)")
    ax_btc.grid(True, axis="y", alpha=0.3)

    ax_m2 = ax_btc.twinx()
    ax_m2.plot(
        combined.index,
        combined["global_m2"],
        color="tab:orange",
        label=m2_label,
    )
    ax_m2.set_ylabel(m2_axis_label)

    lines = ax_btc.get_lines() + ax_m2.get_lines()
    labels = [line.get_label() for line in lines]
    ax_btc.legend(lines, labels, loc="upper left")

    ax_corr.plot(
        combined.index,
        combined["rolling_corr"],
        color="tab:green",
        label=corr_label,
    )
    ax_corr.axhline(0, color="gray", linewidth=1, alpha=0.6)
    ax_corr.set_ylabel("Correlation")
    ax_corr.set_ylim(-1.0, 1.0)
    ax_corr.grid(True, axis="y", alpha=0.3)
    ax_corr.legend(loc="upper left")

    ax_btc.set_title(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def main() -> None:
    config = load_config()
    output_dir = Path(config["outputDir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(config["logPath"])
    configure_logging(log_path, config["logRotation"])

    logger.info("Fetching global M2 from MacroMicro.")
    global_m2_raw, unit = fetch_global_m2_macromicro(config["macroMicro"])
    global_m2 = normalize_month_start(global_m2_raw)

    logger.info("Fetching BTC price history.")
    btc_daily = fetch_btc_prices()
    btc_monthly = monthly_last(btc_daily)

    lead_months = int(config["leadMonths"])
    global_m2_lead = apply_lead_months(global_m2, lead_months)

    combined = align_series(global_m2_lead, btc_monthly)
    combined = add_rolling_correlation(combined, int(config["correlationWindowMonths"]))
    combined = focus_last_years(combined, int(config["focusYears"]))
    logger.info(
        f"Aligned data range: {combined.index.min().date()} to {combined.index.max().date()}"
    )

    chart_path = output_dir / "global_m2_vs_btc.png"
    csv_path = output_dir / "global_m2_vs_btc.csv"
    combined.to_csv(csv_path, index_label="date")
    logger.info(f"Saved combined data to {csv_path}")

    if unit is None:
        m2_label = "Global M2 (MacroMicro, 3m lead)"
    else:
        m2_label = f"Global M2 ({unit}, 3m lead)"
    corr_label = f"{config['correlationWindowMonths']}m rolling correlation"

    focus_years = int(config["focusYears"])
    plot_series(
        combined,
        chart_path,
        f"Global M2 (MacroMicro) led 3 months vs BTC price (last {focus_years} years)",
        m2_label,
        m2_label,
        corr_label,
    )
    logger.info(f"Saved chart to {chart_path}")


if __name__ == "__main__":
    main()
