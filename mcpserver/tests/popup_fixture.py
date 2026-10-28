"""Fixture: a main window plus a transient dialog, for popup-detection tests.

    python popup_fixture.py                # main window only; the button opens the dialog
    python popup_fixture.py --dialog       # open the transient dialog right away
    python popup_fixture.py --dialog --passive   # keep the dialog visible but focus the owner
    python popup_fixture.py --seconds 20   # auto-close after N seconds (default 20)

``dialog.transient(main)`` makes Windows record the main window as the dialog's
owner, so ``sysres.popup_for(main_hwnd)`` should return the dialog. Titles are
fixed constants so the selftest can find the windows without parsing stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

import tkinter as tk

MAIN_TITLE = "CU Popup Fixture Main"
DIALOG_TITLE = "CU Popup Fixture Dialog"


def _show_dialog(main: tk.Tk, passive: bool = False) -> None:
    for child in main.winfo_children():
        if isinstance(child, tk.Toplevel):
            child.lift()
            return
    dialog = tk.Toplevel(main)
    dialog.title(DIALOG_TITLE)
    dialog.geometry("360x180+420+340")
    dialog.transient(main)  # owner window
    tk.Label(dialog, text="fixture dialog", font=("Segoe UI", 14)).pack(pady=18)
    tk.Button(dialog, text="Close", command=dialog.destroy).pack()
    dialog.lift()
    dialog.focus_force()  # real dialogs open active; detection also accepts the owner/foreground path
    if passive:
        # Visible owned dialog that never keeps focus: only owner enumeration finds it.
        main.after(300, lambda: (main.lift(), main.focus_force()))
    print(json.dumps({"event": "dialog", "title": DIALOG_TITLE}), flush=True)


def build(open_dialog: bool, seconds: float, passive: bool = False) -> tk.Tk:
    main = tk.Tk()
    main.title(MAIN_TITLE)
    main.geometry("520x320+320+240")
    tk.Label(main, text="fixture main window", font=("Segoe UI", 14)).pack(pady=16)
    tk.Button(main, text="Open dialog", command=lambda: _show_dialog(main, passive)).pack()
    tk.Button(main, text="Quit", command=main.destroy).pack(pady=8)
    if open_dialog:
        main.after(300, lambda: _show_dialog(main, passive))
    main.after(int(max(1.0, seconds) * 1000), main.destroy)
    print(json.dumps({"event": "main", "title": MAIN_TITLE}), flush=True)
    return main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialog", action="store_true", help="open the transient dialog immediately")
    parser.add_argument("--passive", action="store_true",
                        help="return focus to the owner while the dialog stays visible")
    parser.add_argument("--seconds", type=float, default=20.0, help="auto-close after N seconds")
    args = parser.parse_args()
    build(args.dialog, args.seconds, args.passive).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
