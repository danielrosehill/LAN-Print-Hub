"""LAN Print Hub: one queue for every printer on the LAN.

POST /jobs              label job (JSON) or document job (multipart: file + fields). ?wait=true blocks until done.
GET  /jobs[?limit=50]   job history, newest first.     GET /jobs/{id}   one job.
GET  /targets           configured printers with live health.
POST /preview           render a label job and return the PNG without printing.
GET  /health            service health.

Jobs run one at a time per target (a worker thread each), so two clients can't interleave on one printer.
State: /data/jobs.db (sqlite), config: /data/targets.yaml (see targets.example.yaml).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import shutil
import sqlite3
import threading
import time
import traceback
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

import drivers
import render

DATA = Path(os.environ.get("HUB_DATA", "/data"))
CONFIG = Path(os.environ.get("HUB_TARGETS", DATA / "targets.yaml"))
DB = DATA / "jobs.db"
SPOOL = DATA / "spool"
KEEP_DAYS = int(os.environ.get("HUB_KEEP_DAYS", "90"))
MAX_COPIES = int(os.environ.get("HUB_MAX_COPIES", "20"))

app = FastAPI(title="LAN Print Hub")
_lock = threading.Lock()
_queues: dict[str, queue.Queue] = {}
_done: dict[str, threading.Event] = {}


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def targets() -> dict:
    cfg = yaml.safe_load(CONFIG.read_text()) or {}
    return cfg.get("targets", {})


def resolve(target: str | None, kind: str) -> tuple[str, dict]:
    ts = targets()
    if target:
        for tid, t in ts.items():
            if target == tid or target in t.get("aliases", []):
                if t.get("kind") != kind:
                    raise HTTPException(400, f"{tid} is a {t.get('kind')} printer, not {kind}")
                return tid, t
        raise HTTPException(404, f"unknown target {target}; known: {sorted(ts)}")
    cands = [(tid, t) for tid, t in ts.items() if t.get("kind") == kind]
    dflt = [c for c in cands if c[1].get("default")] or cands[:1]
    if not dflt:
        raise HTTPException(400, f"no {kind} printer configured")
    return dflt[0]


# ---------------- store ----------------

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    SPOOL.mkdir(exist_ok=True)
    with db() as c:
        c.execute("""create table if not exists jobs(
            id text primary key, created text, updated text, target text, kind text, state text,
            client text, summary text, request text, result text, error text)""")
        # Jobs that were queued or running when the hub stopped will never finish.
        c.execute("update jobs set state='failed', error='hub restarted before the job ran', updated=? "
                  "where state in ('queued','printing')", (now(),))
        cutoff = (dt.datetime.now() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        c.execute("delete from jobs where created < ?", (cutoff,))
    shutil.rmtree(SPOOL, ignore_errors=True)
    SPOOL.mkdir()


def save(job_id, **f):
    f["updated"] = now()
    with db() as c:
        c.execute(f"update jobs set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), job_id))


def row(job_id):
    with db() as c:
        r = c.execute("select * from jobs where id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no such job")
    d = dict(r)
    for k in ("request", "result"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


# ---------------- workers ----------------

def worker(tid: str):
    q = _queues[tid]
    while True:
        job_id, job = q.get()
        save(job_id, state="printing")
        try:
            t = targets()[tid]
            drv = drivers.driver_for(t)
            if job["kind"] == "document":
                res = drv.print_document(Path(job["path"]), job["filename"], job)
            else:
                img = label_image(drv, t, job)
                res = drv.print_image(img, job)
            save(job_id, state="done", result=json.dumps(res))
        except Exception as e:  # noqa: BLE001
            save(job_id, state="failed", error=f"{type(e).__name__}: {e}"[:1000])
            traceback.print_exc()
        finally:
            if job.get("path"):
                shutil.rmtree(Path(job["path"]).parent, ignore_errors=True)
            ev = _done.pop(job_id, None)
            if ev:
                ev.set()
            q.task_done()


def label_image(drv, t, job):
    if job.get("png"):
        img = drivers.decode_png(job["png"])
        if t["driver"] == "niimblue":  # fit an arbitrary image onto the die-cut label
            blank = drv.render({**job, "text": "", "icon": None})
            img.thumbnail(blank.size)
            canvas = render.Image.new("L", blank.size, 255)
            canvas.paste(img, ((blank.width - img.width) // 2, (blank.height - img.height) // 2))
            img = canvas
        return img
    if not job.get("text") and not job.get("icon"):
        raise drivers.DriverError("a label job needs text, icon or png")
    return drv.render(job)


def enqueue(tid, kind, job, client, summary):
    job_id = uuid.uuid4().hex[:12]
    req = {k: v for k, v in job.items() if k not in ("png", "path")}
    with db() as c:
        c.execute("insert into jobs values(?,?,?,?,?,?,?,?,?,?,?)",
                  (job_id, now(), now(), tid, kind, "queued", client, summary, json.dumps(req), None, None))
    with _lock:
        if tid not in _queues:
            _queues[tid] = queue.Queue()
            threading.Thread(target=worker, args=(tid,), daemon=True, name=f"worker-{tid}").start()
    _done[job_id] = threading.Event()
    _queues[tid].put((job_id, {**job, "kind": kind}))
    return job_id


# ---------------- API ----------------

@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"ok": True, "targets": sorted(targets()), "time": now()}


@app.get("/targets")
def list_targets():
    out = []
    for tid, t in targets().items():
        try:
            h = drivers.driver_for(t).health()
        except Exception as e:  # noqa: BLE001
            h = {"ok": False, "error": str(e)}
        out.append({"id": tid, "kind": t.get("kind"), "driver": t.get("driver"), "name": t.get("name"),
                    "location": t.get("location"), "default": bool(t.get("default")),
                    "aliases": t.get("aliases", []), "health": h})
    return {"targets": out}


@app.get("/jobs")
def jobs(limit: int = 50, target: str | None = None):
    q, args = "select id, created, updated, target, kind, state, client, summary, error from jobs", []
    if target:
        q += " where target=?"
        args.append(target)
    q += " order by created desc limit ?"
    args.append(min(limit, 500))
    with db() as c:
        return {"jobs": [dict(r) for r in c.execute(q, args)]}


@app.get("/jobs/{job_id}")
def job(job_id: str):
    return row(job_id)


def _wait(job_id, wait, timeout=300):
    ev = _done.get(job_id)
    if wait and ev:
        ev.wait(timeout)
    return row(job_id)


@app.post("/jobs")
async def submit(request: Request, wait: bool = False):
    ctype = request.headers.get("content-type", "")
    client = request.client.host if request.client else "?"
    if ctype.startswith("multipart/"):
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "filename"):
            raise HTTPException(400, "multipart document jobs need a 'file' field")
        tid, t = resolve(form.get("target"), "document")
        fields = {k: v for k, v in form.items() if k != "file"}
        copies = int(fields.get("copies", 1))
        if copies > MAX_COPIES:
            raise HTTPException(400, f"{copies} copies exceeds the cap of {MAX_COPIES}")
        d = drivers.tmpdir()
        path = d / Path(up.filename or "document").name
        path.write_bytes(await up.read())
        job = {"path": str(path), "filename": path.name, "copies": copies,
               "sides": fields.get("sides"), "mono": str(fields.get("mono", "")).lower() in ("1", "true", "yes"),
               "pages": fields.get("pages"), "media": fields.get("media"),
               "fit": str(fields.get("fit", "")).lower() in ("1", "true", "yes")}
        job_id = enqueue(tid, "document", job, client, path.name)
        return _wait(job_id, wait)
    body = await request.json()
    tid, t = resolve(body.get("target"), "label")
    if int(body.get("copies", 1)) > MAX_COPIES:
        raise HTTPException(400, f"copies exceeds the cap of {MAX_COPIES}")
    summary = (body.get("text") or "(image)").replace("\\n", " / ").replace("\n", " / ")[:120]
    job_id = enqueue(tid, "label", body, client, summary)
    return _wait(job_id, wait)


@app.post("/preview")
async def preview(request: Request):
    body = await request.json()
    tid, t = resolve(body.get("target"), "label")
    try:
        img = label_image(drivers.driver_for(t), t, body)
    except (drivers.DriverError, render.RenderError) as e:
        raise HTTPException(400, str(e))
    return Response(render.png_bytes(img), media_type="image/png",
                    headers={"X-Target": tid, "X-Size-Px": f"{img.width}x{img.height}"})
