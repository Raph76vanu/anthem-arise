"""Compare EXM playback hypotheses on an extracted Anthem animation RES.

Usage: python diagnose_exm_animation.py animation.res exm_skeleton.ebx rigamate.res output_folder
"""
import argparse
from pathlib import Path
import sys

from game_asset_explorer.animation_diagnostics import diagnose


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("animation_res", type=Path, nargs="?")
    parser.add_argument("skeleton_ebx", type=Path, nargs="?")
    parser.add_argument("rigamate_res", type=Path, nargs="?")
    parser.add_argument("output_folder", type=Path, nargs="?")
    args = parser.parse_args()
    if len(sys.argv) == 1:
        from tkinter import Tk, filedialog
        root = Tk()
        root.withdraw()
        try:
            files = [filedialog.askopenfilename(title=title, filetypes=[("All files", "*.*")])
                     for title in (
                         "Select the extracted EXM animation .res",
                         "Select the extracted exm_skeleton.ebx",
                         "Select the extracted Rigamate bank .res",
                     )]
        finally:
            root.destroy()
        if not all(files):
            print("Cancelled; no files were changed.")
            return
        animation_res, skeleton_ebx, rigamate_res = map(Path, files)
        output_folder = Path.cwd() / "animation_diagnostics"
    else:
        if any(value is None for value in (
            args.animation_res, args.skeleton_ebx, args.rigamate_res, args.output_folder,
        )):
            parser.error("Provide all four paths or none to use the Windows file dialogs.")
        animation_res, skeleton_ebx, rigamate_res, output_folder = (
            args.animation_res, args.skeleton_ebx, args.rigamate_res, args.output_folder
        )
    rows = diagnose(animation_res, skeleton_ebx, rigamate_res, output_folder)
    print(f"Wrote {len(rows)} clip/variant measurements and comparison sheets to {output_folder}")


if __name__ == "__main__":
    main()
