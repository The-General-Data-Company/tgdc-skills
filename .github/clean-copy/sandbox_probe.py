"""CI only, never on a real drive: it writes a probe file into every folder it is given. The safety probes of Phase 0
of tgdc-clean-copy, run inside an agent's sandbox, plus both shipped detectors on the test files of the first folder.

Run: python sandbox_probe.py <skill scripts folder> <home folder> <source folder>... from inside the run folder,
under the sandbox.
Prints one line per check: <check> <result>, including whether each tool of Phase 0 runs. Exits non-zero if a write
outside the run folder or a network request succeeds, or if a source folder cannot be read."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

TOOLS = {"tesseract": ["--list-langs"], "pdftotext": ["-v"], "pdftoppm": ["-v"], "qpdf": ["--version"],
         "exiftool": ["-ver"], "7z": ["i"], "readpst": ["-V"], "antiword": ["-h"]}


def attempt(action):
    try:
        action()
        return "allowed"
    except Exception as exc:
        return "blocked:" + type(exc).__name__


def write(folder):
    def action():
        target = Path(folder) / "clean-copy-probe.txt"
        target.write_text("probe", encoding="utf-8")
        target.unlink()
    return action


def main():
    scripts, home, sources = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
    results = {
        "home_write": attempt(write(home)),
        "temp_write": attempt(write(tempfile.gettempdir())),
        "network_http": attempt(lambda: urllib.request.urlopen("https://example.com", timeout=10).read(1)),
        "network_socket": attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=10).close()),
        "run_folder_write": attempt(write(os.getcwd())),
    }
    for i, source in enumerate(sources):
        try:
            results[f"source{i}_entries"] = str(len(os.listdir(source)))
        except Exception as exc:
            results[f"source{i}_entries"] = "error:" + type(exc).__name__
        results[f"source{i}_write"] = attempt(write(source))
    for tool, flags in TOOLS.items():
        found = shutil.which(tool)
        try:
            run = subprocess.run([found, *flags], capture_output=True, text=True, timeout=60) if found else None
            output = (run.stdout + run.stderr).split() if run else []
            results["tool_" + tool] = (f"runs:{run.returncode}:" + ",".join(output[:12])) if run else "not_found"
        except Exception as exc:
            results["tool_" + tool] = "error:" + type(exc).__name__
    if sources:
        passport, staff = Path(sources[0]) / "Ausweis.pdf", Path(sources[0]) / "Lohn.csv"
        for script, file in (("id_signals.py", passport), ("table_signals.py", staff)):
            try:
                listing = Path(f"{script}.tsv")
                listing.write_text(f"F000001\t{file}\n", encoding="utf-8")
                run = subprocess.run([sys.executable, str(scripts / script), str(listing), f"{script}.csv",
                                      f"{script}.log"], capture_output=True, text=True, timeout=300)
                results[script] = (run.stdout.strip() or run.stderr.strip()[-200:]).replace(" ", ",")
            except Exception as exc:
                results[script] = "error:" + type(exc).__name__
    for key, value in results.items():
        print(key, value, flush=True)
    unsafe = [k for k in ("home_write", "network_http", "network_socket") if results[k] == "allowed"]
    unsafe += [k for k in results if k.endswith("_write") and k.startswith("source") and results[k] == "allowed"]
    unreadable = [k for k in results if k.endswith("_entries") and results[k].startswith("error")]
    sys.exit(1 if unsafe or unreadable else 0)


if __name__ == "__main__":
    main()
