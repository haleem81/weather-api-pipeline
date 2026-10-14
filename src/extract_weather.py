import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

FORECAST_URL = "https://api.open-meteo.invalid/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARIABLES = "temperature_2m_max,temperature_2m_min,precipitation_sum"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
ARCHIVE_DELAY_DAYS = 5
PAUSE_BETWEEN_CALLS = 0.5
OUTPUT_DIR = os.path.join("data", "raw")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weather")

def load_cities(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_json(url, params, max_attempts=5, base_delay=1.0, timeout=30):
    for attempt in range(1, max_attempts + 1):
        retry_after = None
        try:
            response = requests.get(url, params=params, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as error:
            reason = f"network error ({error.__class__.__name__})"
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code not in RETRYABLE_STATUS:
                raise RuntimeError(f"HTTP {response.status_code} (not retryable): {response.text[:300]}")
            reason = f"HTTP {response.status_code}"
            retry_after = response.headers.get("Retry-After")

        if attempt == max_attempts:
            raise RuntimeError(f"Giving up after {max_attempts} attempts ({reason})")

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        if retry_after and retry_after.isdigit():
            delay = max(delay, int(retry_after))
        log.warning("Attempt %s/%s failed (%s). Retrying in %.1fs", attempt, max_attempts, reason, delay)
        time.sleep(delay)

def build_record(endpoint, city, params, body):
    return {
        "source": "open-meteo",
        "endpoint": endpoint,
        "city": city["name"],
        "request_params": params,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "response": body,
    }


def extract_forecast(city, forecast_days=7):
    params = {
        "latitude": city["latitude"],
        "longitude": city["longitude"],
        "daily": DAILY_VARIABLES,
        "timezone": city["timezone"],
        "forecast_days": forecast_days,
    }
    body = fetch_json(FORECAST_URL, params)
    return build_record("forecast", city, params, body)


def extract_history(city, start_date, end_date):
    params = {
        "latitude": city["latitude"],
        "longitude": city["longitude"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": DAILY_VARIABLES,
        "timezone": city["timezone"],
    }
    body = fetch_json(ARCHIVE_URL, params)
    return build_record("archive", city, params, body)

def save_local(record, relative_path):
    path = os.path.join(OUTPUT_DIR, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(temp_path, path)
    return path

def validate_history_dates(start, end):
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    latest_ok = datetime.now(timezone.utc).date() - timedelta(days=ARCHIVE_DELAY_DAYS)
    if start_d > end_d:
        raise ValueError("--start must be on or before --end")
    if end_d > latest_ok:
        raise ValueError(f"--end must be on or before {latest_ok} (archive data lags by about {ARCHIVE_DELAY_DAYS} days)")


def slug(name):
    return name.strip().lower().replace(" ", "_")


def main():
    parser = argparse.ArgumentParser(description="Extract weather data from Open-Meteo")
    parser.add_argument("--mode", choices=["forecast", "history"], default="forecast")
    parser.add_argument("--start", help="History start date, YYYY-MM-DD")
    parser.add_argument("--end", help="History end date, YYYY-MM-DD")
    parser.add_argument("--city", help="Only extract this city")
    parser.add_argument("--config", default="config/cities.json")
    args = parser.parse_args()

    if args.mode == "history":
        if not args.start or not args.end:
            parser.error("--start and --end are required in history mode")
        try:
            validate_history_dates(args.start, args.end)
        except ValueError as error:
            parser.error(str(error))

    cities = load_cities(args.config)
    today = datetime.now(timezone.utc).date().isoformat()
    succeeded, failed = 0, []

    for city in cities:
        if args.city and city["name"].lower() != args.city.lower():
            continue
        try:
            if args.mode == "forecast":
                record = extract_forecast(city)
                relative = f"source=open_meteo/endpoint=forecast/dt={today}/city={slug(city['name'])}.json"
            else:
                record = extract_history(city, args.start, args.end)
                relative = (f"source=open_meteo/endpoint=archive/"
                            f"start={args.start}_end={args.end}/city={slug(city['name'])}.json")
            path = save_local(record, relative)
            log.info("Saved %s", path)
            succeeded += 1
        except Exception as error:
            log.error("Failed for %s: %s", city["name"], error)
            failed.append(city["name"])
        time.sleep(PAUSE_BETWEEN_CALLS)

    log.info("Done: %s succeeded, %s failed", succeeded, len(failed))
    if failed:
        log.error("Failed cities: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()