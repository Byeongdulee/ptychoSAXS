import requests
import json
import time
import winsound
duration = 5000 # milliseconds
freq = 440 # Hz

status_url = 'https://12idb.xray.aps.anl.gov/PVapp/ptycho_status'

def fetch_status():
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
        return data
if __name__ == "__main__":
    while True:
        data = fetch_status()
        time.sleep(60*2)  # wait for 2 minutes before next fetch
        if data["recent error message"] != "":
            winsound.Beep(freq, duration)