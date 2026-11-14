"""Printer drivers. Each takes a target config dict (from targets.yaml) and a job, and returns a result dict.

  cups           document printers: `lp` against a CUPS server (default localhost:631).
  niimblue       NIIMBOT label printers behind a niimblue-node REST server (/connected, /scan, /connect, /print).
  ptouch-bridge  Brother P-touch behind a ptouch-print HTTP bridge (GET /health -> maxPx/tapeMm, POST /print).
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

import render


class DriverError(RuntimeError):
    pass


def http(url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise DriverError(f"{url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DriverError(f"{url} unreachable: {getattr(e, 'reason', e)}")


# ---------------- CUPS ----------------

def _run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise DriverError(f"{' '.join(cmd[:3])}...: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


class Cups:
    def __init__(self, t):
        self.t = t
        self.server = t.get("cups", "localhost:631")
        self.queue = t["queue"]

    def health(self):
        out = subprocess.run(["lpstat", "-h", self.server, "-p", self.queue], capture_output=True, text=True, timeout=15)
        txt = (out.stdout or out.stderr).strip()
        return {"ok": out.returncode == 0 and "disabled" not in txt, "cups": self.server, "queue": self.queue, "state": txt[:300]}

    def print_document(self, path: Path, filename: str, opts: dict):
        src = path
        # Known fault (2026-11-13): the snap CUPS 2.4.20 on residencehome breaks PDFs in pdftopdf/gstoraster
        # ("Unexpected page count"). PostScript prints fine, so convert PDFs first unless the target opts out.
        if self.t.get("pdf_as_ps", False) and (filename.lower().endswith(".pdf") or path.read_bytes()[:5] == b"%PDF-"):
            ps = path.with_suffix(".ps")
            _run(["pdftops", "-level3", "-paper", opts.get("media") or self.t.get("paper", "A4"), str(path), str(ps)])
            src = ps
        cmd = ["lp", "-h", self.server, "-d", self.queue, "-n", str(int(opts.get("copies", 1))), "-t", filename]
        sides = {"one": "one-sided", "long": "two-sided-long-edge", "short": "two-sided-short-edge"}
        if opts.get("sides"):
            cmd += ["-o", f"sides={sides.get(opts['sides'], opts['sides'])}"]
        if opts.get("mono"):
            cmd += ["-o", "print-color-mode=monochrome"]
        if opts.get("pages"):
            cmd += ["-o", f"page-ranges={opts['pages']}"]
        if opts.get("media"):
            cmd += ["-o", f"media={opts['media']}"]
        if opts.get("fit"):
            cmd += ["-o", "fit-to-page"]
        cmd.append(str(src))
        out = _run(cmd)
        m = re.search(r"request id is (\S+)", out)
        return {"ok": True, "cups_job": m.group(1) if m else None, "converted_to_ps": src != path}

    def print_label(self, *a, **k):
        raise DriverError("this target is a document printer; send a document")


# ---------------- NIIMBOT (niimblue-node) ----------------

class Niimblue:
    def __init__(self, t):
        self.t = t
        self.url = t["bridge"].rstrip("/")

    def health(self):
        try:
            c = http(self.url + "/connected", timeout=8)
            return {"ok": True, "bridge": self.url, "connected": bool(c.get("connected")), "media": self.t.get("media")}
        except DriverError as e:
            return {"ok": False, "bridge": self.url, "error": str(e)}

    def _connect(self):
        if http(self.url + "/connected", timeout=10).get("connected"):
            return
        port = self.t.get("port")
        if not port:
            devs = [d["address"] for d in http(self.url + "/scan", {"transport": "serial", "timeout": 3000}).get("devices", [])]
            port = next((d for p in ("/dev/serial/by-id/", "/dev/ttyACM", "/dev/ttyUSB") for d in devs if d.startswith(p)), None)
            if not port:
                raise DriverError("no NIIMBOT serial device on the bridge host (plugged in and switched on?)")
        http(self.url + "/connect", {"transport": "serial", "address": port}, timeout=30)

    def render(self, job):
        media = job.get("media") or self.t.get("media", "40x30")
        sizes = self.t.get("media_sizes", {})
        if media in sizes:
            across, along = sizes[media]["across"], sizes[media]["along"]
        else:
            across, along = (float(x) for x in media.lower().split("x"))
        return render.diecut(
            job.get("text", ""), job.get("icon"), across, along,
            head_px=int(self.t.get("head_px", 384)),
            layout=job.get("layout", "auto"),
            margin_mm=float(job.get("margin_mm", self.t.get("margin_mm", 1.0))),
            offset_mm=float(job.get("offset_mm", self.t.get("offset_mm", 0))),
            icon_ratio=float(self.t.get("icon_ratio", 0.45)),
            invert=bool(job.get("invert")), border=bool(job.get("border")),
        )

    def print_image(self, img: Image.Image, job):
        head = int(self.t.get("head_px", 384))
        if img.width > head:
            img = img.crop((0, 0, head, img.height))
        self._connect()
        res = http(self.url + "/print", {
            "printTask": self.t.get("print_task", "B1"),
            "printDirection": "top",
            "imageBase64": base64.b64encode(render.png_bytes(img)).decode(),
            "labelWidth": img.width, "labelHeight": img.height,
            "quantity": int(job.get("copies", 1)),
            "density": int(job.get("density", self.t.get("density", 3))),
            "waitUntilFinished": True,
        }, timeout=180)
        return {"ok": True, "bridge_result": res, "size_px": list(img.size)}


# ---------------- Brother P-touch (ptouch-print bridge) ----------------

class PtouchBridge:
    def __init__(self, t):
        self.t = t
        self.url = t["bridge"].rstrip("/")

    def health(self):
        try:
            return {"bridge": self.url, **http(self.url + "/health", timeout=10)}
        except DriverError as e:
            return {"ok": False, "bridge": self.url, "error": str(e)}

    def render(self, job):
        h = self.health()
        px = h.get("maxPx") or self.t.get("height_px")
        if not px:
            raise DriverError(f"P-touch bridge gave no tape height: {h}")
        return render.tape(job.get("text", ""), job.get("icon"), int(px), invert=bool(job.get("invert")))

    def print_image(self, img: Image.Image, job):
        url = "data:image/png;base64," + base64.b64encode(render.png_bytes(img)).decode()
        res = http(self.url + "/print", {"imageDataUrl": url, "copies": int(job.get("copies", 1))}, timeout=240)
        if not res.get("ok"):
            raise DriverError(f"bridge refused: {res}")
        return {"ok": True, "bridge_result": res, "size_px": list(img.size)}


DRIVERS = {"cups": Cups, "niimblue": Niimblue, "ptouch-bridge": PtouchBridge}


def driver_for(target: dict):
    cls = DRIVERS.get(target.get("driver"))
    if not cls:
        raise DriverError(f"unknown driver {target.get('driver')!r}")
    return cls(target)


def decode_png(b64: str) -> Image.Image:
    import io
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("L")


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="hubjob-"))
