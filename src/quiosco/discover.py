import json
from pathlib import Path

import pychromecast

_OUTPUT_PATH = Path(__file__).resolve().parents[2] / "chromecast.json"


def main() -> None:
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

    with open(_OUTPUT_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nGuardados {len(data)} Chromecast(s) en {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
