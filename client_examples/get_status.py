import requests
import json
status_url = 'https://12idb.xray.aps.anl.gov/PVapp/ptycho_status'
try:
    resp = requests.get(status_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
except (requests.RequestException, ValueError) as e:
    print(f"Error fetching/parsing JSON: {e}")
else:
    #print(json.dumps(data, indent=2, sort_keys=True))
    data =eval(data["status"])
    for key, value in data.items():
        print(f"{key}: {value}")