"""
start_split.py — Lanza API + Worker como dos procesos independientes.

Uso (desarrollo / Windows sin NSSM):
    python start_split.py

Para detener: Ctrl+C (envía SIGTERM a ambos procesos)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent / "facebook_auto_poster"
ENV = {**os.environ, "SPLIT_PROCESSES": "1"}


def main() -> None:
    procs = []
    try:
        api_proc = subprocess.Popen(
            [sys.executable, str(BASE / "api_main.py")],
            env=ENV,
            cwd=str(BASE),
        )
        procs.append(("API", api_proc))
        print(f"[start_split] API   PID={api_proc.pid}")

        worker_proc = subprocess.Popen(
            [sys.executable, str(BASE / "worker_main.py")],
            env=ENV,
            cwd=str(BASE),
        )
        procs.append(("Worker", worker_proc))
        print(f"[start_split] Worker PID={worker_proc.pid}")

        print("[start_split] Ambos procesos arrancados. Ctrl+C para detener.")
        for name, p in procs:
            p.wait()
            print(f"[start_split] {name} terminó (código={p.returncode})")

    except KeyboardInterrupt:
        print("\n[start_split] Deteniendo procesos...")
        for name, p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:
                pass
        for name, p in procs:
            p.wait(timeout=10)
        print("[start_split] Listo.")


if __name__ == "__main__":
    main()
