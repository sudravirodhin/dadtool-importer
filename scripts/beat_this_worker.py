r"""beat_this beat/downbeat worker — runs INSIDE the isolated `dad-beat` conda env
(torch + beat_this). Invoked as a subprocess by dadtool (3.12 librosa env):

    <miniforge>\envs\dad-beat\python.exe scripts\beat_this_worker.py "<audio path>"

Prints ONE line of JSON to stdout (all library chatter is routed to stderr so
stdout stays clean):
    {"beats": [<s>...], "downbeats": [<s>...], "bpm_median": <float>}
"""
import contextlib
import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: beat_this_worker.py <audio>\n")
        sys.exit(2)
    path = sys.argv[1]

    import numpy as np

    # Route any model-loading / download / torch prints to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        from beat_this.inference import File2Beats
        f2b = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        beats, downbeats = f2b(path)

    beats = [float(t) for t in beats]
    downbeats = [float(t) for t in downbeats]
    if len(beats) >= 3:
        ibi = np.diff(np.array(beats))
        ibi = ibi[(ibi > 0.15) & (ibi < 1.5)]
        bpm_median = float(60.0 / np.median(ibi)) if ibi.size else 0.0
    else:
        bpm_median = 0.0

    sys.stdout.write(json.dumps({"beats": beats, "downbeats": downbeats, "bpm_median": bpm_median}))


if __name__ == "__main__":
    main()
