import re
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

PAGE_URL = "https://www.transport.nsw.gov.au/future-trackwork-calendar"
OUTPUT_DIR = "trackwork_calendars"

# Headers to mimic a real web browser and bypass the 403 Forbidden firewall blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1"
}

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]
MONTH_MAP = {name.lower(): i for i, name in enumerate(MONTHS, 1)}

# Known Sydney Trains, Metro, and Intercity line prefixes/names
KNOWN_LINES = [
    r"M1\s+Metro[^\n]*",
    r"City\s+Circle",
    r"T[1-9]\s+[^\n]+",
    r"Blue\s+Mountains\s+Line",
    r"Central\s+Coast\s+&\s+Newcastle\s+Line",
    r"Hunter\s+Line",
    r"South\s+Coast\s+Line",
    r"Southern\s+Highlands\s+Line",
]
LINE_REGEX = re.compile(rf"^({'|'.join(KNOWN_LINES)})$", re.IGNORECASE)

# Header date pattern
MONTH_NAMES_PATTERN = "|".join(MONTHS)
DATE_HEADER_REGEX = re.compile(
    rf"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+.*?(?:{MONTH_NAMES_PATTERN})\b.*?(?:\(.*?\))?$",
    re.IGNORECASE
)


def get_latest_pdf_url() -> str:
    """Scrapes the Transport NSW page for the dynamic PDF link."""
    print(f"Fetching calendar page: {PAGE_URL}")
    resp = requests.get(PAGE_URL, headers=HEADERS)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    # Find link with text matching "Future Trackwork Calendar"
    pdf_link = None
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if "future trackwork calendar" in text.lower() and a["href"].lower().endswith(".pdf"):
            pdf_link = a["href"]
            break
        elif a["href"].lower().endswith(".pdf") and "trackwork" in a["href"].lower():
            pdf_link = a["href"]

    if not pdf_link:
        raise RuntimeError("Could not find the Future Trackwork Calendar PDF link on the page.")

    return urljoin(PAGE_URL, pdf_link)


def download_pdf(url: str, output_path: str = "latest_trackwork.pdf") -> str:
    """Downloads the PDF to a local file."""
    print(f"Downloading PDF from: {url}")
    resp = requests.get(url, headers=HEADERS, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return output_path


def clean_pdf_lines(pdf_path: str) -> list[str]:
    """Reads PDF and strips boilerplate NSW Transport headers/footers."""
    reader = PdfReader(pdf_path)
    all_lines = []

    boilerplate = [
        "future planned trackwork calendar",
        "this future trackwork summary outlines",
        "we try to keep this information",
        "if you are planning to travel within",
        "late changes to this information can occur",
        "visit transportnsw.info",
        "nsw government",
        "transport",
        "trackwork",
        "page ",
    ]

    for page in reader.pages:
        text = page.extract_text()
        if not text:
            continue
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            lower = line_str.lower()
            # Ignore standard repetitive boilerplates and standalone numbers
            if any(b in lower for b in boilerplate) or re.match(r"^\d{2}\s+\d{2}\s+\d{2}$|^\d+$", line_str):
                continue
            all_lines.append(line_str)

    return all_lines


def parse_date_header(header: str, current_year: int) -> tuple[datetime, datetime, str]:
    """
    Parses complex date lines, e.g.:
    - "Saturday 24 and Sunday 25 October (weekend)"
    - "Monday 30 November to Thursday 3 December (evening)"
    - "Tuesday 8 September (daytime)"
    """
    period = ""
    period_match = re.findall(r"\(([^)]+)\)", header)
    if period_match:
        period = ", ".join(period_match)

    # Clean off trailing qualifiers like (evening), (weekend)
    cleaned = re.sub(r"\(.*?\)", "", header).strip()

    # Search for all mentions of (day, month)
    tokens = re.findall(rf"(\d{{1,2}})?\s*(?:to|and)?\s*(\d{{1,2}})\s+({MONTH_NAMES_PATTERN})", cleaned, re.IGNORECASE)

    start_date = None
    end_date = None

    if len(tokens) == 2:
        # Range spanning across months, e.g. "30 November to 3 December"
        d1, m1 = int(tokens[0][1]), MONTH_MAP[tokens[0][2].lower()]
        d2, m2 = int(tokens[1][1]), MONTH_MAP[tokens[1][2].lower()]
        start_year = current_year
        end_year = current_year + 1 if m2 < m1 else current_year
        start_date = datetime(start_year, m1, d1)
        end_date = datetime(end_year, m2, d2)

    elif len(tokens) == 1:
        # Same month: e.g. "Monday 7 and Tuesday 8 September" or "7 to 9 September"
        first_day_str, last_day_str, month_str = tokens[0]
        m = MONTH_MAP[month_str.lower()]
        d2 = int(last_day_str)

        # Check if an earlier day exists before "to" / "and"
        earlier_day_match = re.search(r"(\d{1,2})\s*(?:to|and)\s*(?:\w+\s+)?\d{1,2}\s+" + month_str, cleaned, re.IGNORECASE)
        if earlier_day_match:
            d1 = int(earlier_day_match.group(1))
        else:
            d1 = d2

        start_date = datetime(current_year, m, d1)
        end_date = datetime(current_year, m, d2)

    else:
        # Fallback single day extraction
        day_match = re.search(rf"(\d{{1,2}})\s+({MONTH_NAMES_PATTERN})", cleaned, re.IGNORECASE)
        if day_match:
            d = int(day_match.group(1))
            m = MONTH_MAP[day_match.group(2).lower()]
            start_date = end_date = datetime(current_year, m, d)

    return start_date, end_date, period


def extract_events(cleaned_lines: list[str]) -> list[dict]:
    """Parses chronological text lines into structured trackwork event dictionaries."""
    events = []
    current_year = datetime.now().year

    first_month_seen = None
    curr_start = None
    curr_end = None
    curr_period = ""
    curr_line = None
    curr_desc = []

    def flush_line_event():
        if curr_line and curr_start and curr_end:
            events.append({
                "line": curr_line,
                "start": curr_start,
                "end": curr_end,
                "period": curr_period,
                "description": " ".join(curr_desc).strip()
            })

    for line in cleaned_lines:
        if DATE_HEADER_REGEX.match(line):
            flush_line_event()
            curr_line = None
            curr_desc = []

            # Check for month rollover
            month_match = re.search(rf"({MONTH_NAMES_PATTERN})", line, re.IGNORECASE)
            if month_match:
                m_num = MONTH_MAP[month_match.group(1).lower()]
                if first_month_seen is None:
                    first_month_seen = m_num
                elif m_num < first_month_seen:
                    current_year = datetime.now().year + 1

            curr_start, curr_end, curr_period = parse_date_header(line, current_year)
            continue

        if curr_start and LINE_REGEX.match(line):
            flush_line_event()
            curr_line = line
            curr_desc = []
            continue

        if curr_line:
            curr_desc.append(line)

    flush_line_event()
    return events


def generate_ics(line_name: str, events: list[dict], output_folder: str):
    """Formats and writes a single .ics calendar file for a specific line."""
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", line_name).replace(" ", "_") + ".ics"
    filepath = os.path.join(output_folder, safe_filename)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Sydney Trackwork//{line_name}//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH"
    ]

    for i, ev in enumerate(events):
        dtstart = ev["start"].strftime("%Y%m%d")
        dtend = (ev["end"] + timedelta(days=1)).strftime("%Y%m%d")

        period_prefix = f"({ev['period'].title()}) " if ev["period"] else ""
        summary = f"{period_prefix}{ev['line']} Trackwork"
        desc = ev["description"].replace("\n", " ").replace(";", r"\;").replace(",", r"\,")

        uid = f"{ev['start'].strftime('%Y%m%d')}-{i}-{safe_filename}@transport.nsw"

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{summary}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"DESCRIPTION:{desc}",
            "STATUS:CONFIRMED",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines) + "\r\n")

    print(f"✓ Created: {filepath} ({len(events)} events)")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Scrape latest PDF URL
    try:
        pdf_url = get_latest_pdf_url()
    except Exception as e:
        print(f"Error finding latest PDF URL: {e}")
        sys.exit(1)

    # 2. Download PDF
    local_pdf = download_pdf(pdf_url)

    # 3. Extract & clean text lines
    cleaned_lines = clean_pdf_lines(local_pdf)

    # 4. Parse events
    events = extract_events(cleaned_lines)
    print(f"\nParsed {len(events)} total trackwork events across all lines.")

    # 5. Group by train line and write ICS files
    events_by_line = {}
    for ev in events:
        events_by_line.setdefault(ev["line"], []).append(ev)

    for line_name, line_events in sorted(events_by_line.items()):
        generate_ics(line_name, line_events, OUTPUT_DIR)

    print(f"\nAll calendar files have been saved to '{OUTPUT_DIR}/'.")


if __name__ == "__main__":
    main()
