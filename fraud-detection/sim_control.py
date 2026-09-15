"""In-process controller for the live sim, so the web app can drive it.

Runs sim ticks on a background thread (append mode — continues the existing
dataset). Start/stop/speed are exposed via the API; status is read from the DB so
it's accurate even across an API reload. A retrain runs every sim-day, matching the
CLI sim, so the model version keeps moving while you watch.
"""

from __future__ import annotations

import random
import threading

import sim as S
from queries import connect, require_label_history


_TICK_INTERVAL = 1.2  # real seconds between ticks (fixed cadence; 15 sim-min per tick)


class SimController:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.speed = 2.0                 # informational only (ticks/sec = 1/_TICK_INTERVAL)
        self.running = False
        self.last = {"made": 0, "landed": 0, "ticks": 0}

    def status(self) -> dict:
        try:
            with connect() as c, c.cursor() as cur:
                cur.execute("SELECT MAX(sim_now) FROM sim_clock")
                sn = cur.fetchone()[0]
        except Exception:
            sn = None
        return {"running": self.running, "speed": self.speed,
                "sim_now": sn.isoformat() if sn else None, **self.last}

    def set_speed(self, speed: float) -> None:
        # kept for API compatibility; has no effect on the fixed cadence
        self.speed = max(0.5, min(float(speed), 60.0))

    def start(self, speed: float | None = None) -> dict:
        with self._lock:
            if speed is not None:
                self.set_speed(speed)
            if not self.running:
                # Fail in the caller before claiming a worker is running on legacy data.
                with connect() as c, c.cursor() as cur:
                    require_label_history(cur)
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self.running = True
                self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        self.running = False
        return self.status()

    def _run(self) -> None:
        import model  # deferred: heavy sklearn import
        rng = random.Random(S.SEED)
        sim = S.Sim(connect(), rng)
        sim.resume()
        n = 0
        while not self._stop.is_set():
            try:
                made, landed = sim.tick()
                n += 1
                self.last = {"made": made, "landed": landed, "ticks": n}
                if n % S.RETRAIN_EVERY == 0:
                    model.train(sim.conn)
            except Exception as e:  # keep the loop alive; a bad tick shouldn't kill the sim
                print("sim tick error:", e)
            self._stop.wait(_TICK_INTERVAL)
        self.running = False


controller = SimController()
