# Pack de assets externo

Directorio de assets propios para la suite de agentes. Se registra en
`~/.pixel-agents/config.json` (`externalAssetDirectories`) y el server hace merge de
`dist/assets` del paquete con lo que haya aqui.

Registrar (una sola vez, ruta absoluta):

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".pixel-agents/config.json"
c = json.loads(p.read_text())
d = str(pathlib.Path.home() / "Code/quiosco/pixel-agents/assets-pack")
if d not in c["externalAssetDirectories"]:
    c["externalAssetDirectories"].append(d)
    p.write_text(json.dumps(c, indent=2))
PY
systemctl --user restart pixel-agents
```

Tambien se puede agregar desde la UI de la app (manda `addExternalAssetDirectory`), lo que
recarga characters, pets y furniture sin reiniciar. Pisos, paredes y alfombras necesitan
restart del service.

El server lee estas rutas desde la **laptop**, no desde exodia.

## Estructura esperada

El paquete busca un subdirectorio `assets/` dentro del pack:

```
assets-pack/assets/
├── characters/     char_0.png .. char_5.png   (112x96, los SEIS o se ignora la carpeta)
├── floors/         floor_*.png                (16x16, un tile)
├── walls/          wall_*.png                 (64x128, set de tiles)
├── carpets/        carpet_*.png               (64x64, set de tiles)
├── pets/<id>/      pet.png (96x96) + manifest.json  -> {"id": "gitcat", "name": "Gitcat"}
└── furniture/<ID>/ manifest.json + PNGs
```

Todo es pixel art sobre grilla de **16x16 px por tile**: un mueble de `footprintW: 3` /
`footprintH: 2` mide 48x32 px.

`furniture/<ID>/manifest.json`, mueble simple:

```json
{
  "id": "CACTUS", "name": "Cactus", "category": "decor", "type": "asset",
  "canPlaceOnWalls": false, "canPlaceOnSurfaces": false, "backgroundTiles": 1,
  "width": 16, "height": 32, "footprintW": 1, "footprintH": 2
}
```

Con rotacion (varias orientaciones del mismo mueble), `type: "group"` + `members[]`, cada
miembro con su `file`, medidas y `orientation` (ver `DESK` en `dist/assets/furniture/DESK/`
del paquete como referencia). `category` conocidas: `desks`, `wall`, `decor`, `misc`
(`desks` marca el mueble como escritorio donde se sienta un agente).

Gotchas:

- Un `id` repetido **sobreescribe** el mueble del paquete. Util para reemplazar sin parchear
  `dist/`, peligroso por accidente.
- Sin `manifest.json` la carpeta del mueble se ignora en silencio (queda un warning en
  `journalctl --user -u pixel-agents`).
- Rutas fuera del directorio del mueble (`../`) se descartan por seguridad.
- `characters/` exige `char_0.png` a `char_5.png` completos; si falta uno, la carpeta entera
  se ignora.
