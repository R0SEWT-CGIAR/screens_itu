import json
import pychromecast

print("Buscando Chromecasts en la red...")
chromecasts, browser = pychromecast.get_chromecasts()
pychromecast.discovery.stop_discovery(browser)

data = []
for cc in chromecasts:
    host = cc.cast_info.host
    port = cc.cast_info.port
    data.append({
        "name": cc.name,
        "host": host,
        "port": port,
        "model_name": cc.model_name,
        "uuid": str(cc.uuid),
    })
    print(f"  - {cc.name} @ {host}")

with open("chromecast.json", "w") as f:
    json.dump(data, f, indent=2)

print(f"\nGuardados {len(data)} Chromecast(s) en chromecast.json")
