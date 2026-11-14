# LAN Print Hub

One print queue for every printer on a home LAN. Clients (CLI tools, agents, Home Assistant, other apps) send
jobs to one HTTP API and name a **target**; the hub queues them per printer, renders labels server-side and
routes each job to the right backend:

| Driver | Printers | How |
|---|---|---|
| `cups` | document printers (A4) | `lp` against a CUPS server; PDFs optionally converted to PostScript first |
| `niimblue` | NIIMBOT die-cut label printers | [niimblue-node](https://github.com/MultiMote/niimblue-node) REST server (`/connect`, `/print`) |
| `ptouch-bridge` | Brother P-touch tape printers | a `ptouch-print` HTTP bridge (`GET /health` → `maxPx`, `POST /print {imageDataUrl, copies}`) |

Jobs run one at a time per target, with history in SQLite. There is no auth: run it on a trusted LAN/tailnet.

## API

```bash
H=http://10.0.0.2:9190
curl $H/targets                                   # printers + live health
# label (JSON). target = id or alias; omit for the default label printer
curl -X POST "$H/jobs?wait=true" -H 'content-type: application/json' \
     -d '{"target":"ptouch","text":"Flat Ethernet\nCables","copies":2}'
curl -X POST "$H/jobs?wait=true" -H 'content-type: application/json' \
     -d '{"target":"niimbot","text":"Screens\nOn / Off","icon":"monitor"}'
# preview a label without printing (returns PNG)
curl -X POST $H/preview -H 'content-type: application/json' -d '{"target":"niimbot","text":"Hall"}' -o p.png
# document (multipart). Fields: target, copies, sides=one|long|short, mono, pages, media, fit
curl -X POST "$H/jobs?wait=true" -F file=@doc.pdf -F target=hp -F sides=long
curl "$H/jobs?limit=20"; curl $H/jobs/<id>
```

Label fields: `text` (`\n` breaks lines), `icon` (Iconify id, bare name = `material-symbols:`), `png` (base64 or
data URL, instead of text), `copies`, `invert`; NIIMBOT also `media` (`40x30`), `layout` (`auto|top|left|icon|text`),
`margin_mm`, `offset_mm`, `density`, `border`.

Job states: `queued` → `printing` → `done` | `failed` (`error` says why). A document job is `done` when CUPS
accepted it (`result.cups_job`); follow it in CUPS from there.

## Configuration

`data/targets.yaml`, see [`targets.example.yaml`](targets.example.yaml). Edits apply to the next job; no restart.
Env: `HUB_PORT` (9190), `HUB_KEEP_DAYS` (job history, 90), `HUB_MAX_COPIES` (20), `HUB_FONT`.
Labels use IBM Plex Sans Bold, downloaded from IBM's GitHub at build time (`fonts-ibm-plex` is not in Debian trixie).

## Deployment (residencehome)

- Host path `/home/daniel/deployments/print-hub/` (this repo, cloned over SSH), registered in `stacks.yaml`
  (Residencehome-Deployments) and `Residenceserver-Admin/stacks.json`.
- URL: `http://10.0.0.2:9190` (LAN), `http://100.105.192.28:9190` (tailnet).
- `network_mode: host`: CUPS only accepts localhost/LAN/tailnet clients, and the NIIMBOT bridge is on the host.
- Data: `./data` (`targets.yaml`, `jobs.db`, icon cache). Local build, so Watchtower is opted out. Update:
  `git pull && docker compose up -d --build`.
- Backends on 2026-11-14: CUPS queue `hp-deskjet-5200` (snap CUPS on the host), `niimblue-bridge.service`
  (host, :9181), P-touch bridge `wms-cube-bridge.service` on the desktop (:9181, tailnet).

## Findings

- Snap CUPS 2.4.20 on the server breaks PDF jobs (`pdftopdf` → `gstoraster` "Unexpected page count"), PostScript
  prints fine: hence `pdf_as_ps: true`.
- The P-touch bridge scales whatever height it receives to the loaded tape, so the hub renders at the bridge's
  reported `maxPx` (128 px for 24 mm tape) to keep text sharp.
- NIIMBOT prints on the 40x30 roll sat against the right edge; `offset_mm: -2` shifts the image 2 mm left.
