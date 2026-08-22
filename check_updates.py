import asyncio
import requests

async def check_updates():
    try:
        r = await asyncio.to_thread(requests.get, "https://codeberg.org/api/v1/repos/liburnb/Touch-Dashboard/tags", timeout=5)
        if r.status_code == 200:
            tags = r.json()
            for tag in tags:
                name = tag.get("name", "")
                if name.startswith("v"):
                    print("Latest version:", name)
                    return name
    except Exception as e:
        print(e)

asyncio.run(check_updates())
