import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
CLIST_USERNAME = os.getenv("CLIST_USERNAME")
CLIST_API = os.getenv("CLIST_API")

# Hardcoded resource IDs for the target platforms to allow single-query fetching
# 102: leetcode.com, 1: codeforces.com, 2: codechef.com
PLATFORM_IDS = {
    "leetcode.com": 102,
    "codeforces.com": 1,
    "codechef.com": 2,
}

def fetch_contests():
    # Define the time window for "today" in local timezone
    local_now = datetime.now().astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_now.replace(hour=23, minute=59, second=59, microsecond=999999)

    resource_ids = ",".join(str(i) for i in PLATFORM_IDS.values())

    params = {
        "username": CLIST_USERNAME,
        "api_key": CLIST_API,
        "resource_id__in": resource_ids,
        "start__gte": local_start.isoformat(),
        "start__lte": local_end.isoformat(),
        "order_by": "start",
        "limit": 100,  # Safe limit to get all contests for today across platforms
    }

    try:
        response = requests.get(
            "https://clist.by/api/v4/json/contest/",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        contests = response.json().get("objects", [])
        print(f"+ Fetched {len(contests)} contests starting today")
        return contests

    except Exception as e:
        print(f"- Failed to fetch contests — {e}")
        return []


def fetch_upcoming_contests():
    from zoneinfo import ZoneInfo
    local_now = datetime.now(ZoneInfo("Asia/Kolkata"))

    resource_ids = ",".join(str(i) for i in PLATFORM_IDS.values())

    params = {
        "username": CLIST_USERNAME,
        "api_key": CLIST_API,
        "resource_id__in": resource_ids,
        "start__gte": local_now.isoformat(),
        "order_by": "start",
        "limit": 25,
    }

    try:
        response = requests.get(
            "https://clist.by/api/v4/json/contest/",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        contests = response.json().get("objects", [])
        print(f"+ Fetched {len(contests)} upcoming contests")
        return contests
    except Exception as e:
        print(f"- Failed to fetch upcoming contests — {e}")
        return []

if __name__ == "__main__":
    contests = fetch_upcoming_contests()
    for c in contests:
        print(c["event"], "|", c["start"], "|", c["resource"])
