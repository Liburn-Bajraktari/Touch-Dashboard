import asyncio
import requests

async def check_updates():
    try:
        r = await asyncio.to_thread(
            requests.get,
            "https://api.github.com/repos/Liburn-Bajraktari/Touch-Dashboard/releases",
            headers={"Accept": "application/vnd.github+json"},
            timeout=5,
        )
        if r.status_code == 200:
            releases = r.json()
            for release in releases:
                name = release.get("tag_name", "")
                if name.startswith("v"):
                    print("Latest version:", name)
                    return name
    except Exception as e:
        print(e)

asyncio.run(check_updates())
