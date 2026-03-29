import json
import os
import os.path as osp
import re
import tempfile

os.environ.setdefault("MPLCONFIGDIR", osp.join(tempfile.gettempdir(), "mplconfig_paper_writer"))
os.environ.setdefault("XDG_CACHE_HOME", tempfile.gettempdir())

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def sorted_run_dirs():
    runs = []
    for name in os.listdir("."):
        if re.fullmatch(r"run_\d+", name) and osp.isdir(name):
            runs.append(name)
    return sorted(runs, key=lambda x: int(x.split("_")[1]))


def load_final(run_dir):
    fp = osp.join(run_dir, "final_info.json")
    if not osp.exists(fp):
        return None, None
    with open(fp, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not obj:
        return None, None
    dataset = next(iter(obj.keys()))
    return dataset, obj[dataset]


def load_all_results(run_dir):
    fp = osp.join(run_dir, "all_results.npy")
    if not osp.exists(fp):
        return {}
    return np.load(fp, allow_pickle=True).item()


def plot_main_metrics(runs, dataset):
    labels = []
    overall_vals, draft_vals, cite_vals = [], [], []
    for run in runs:
        _, ds = load_final(run)
        if ds is None:
            continue
        means = ds.get("means", {})
        labels.append(run)
        overall_vals.append(means.get("overall_score_mean", np.nan))
        draft_vals.append(means.get("draft_quality_mean", np.nan))
        cite_vals.append(means.get("citation_coverage_mean", np.nan))

    if not labels:
        print("No valid run data found for summary metrics.")
        return

    x = np.arange(len(labels))
    w = 0.25
    plt.figure(figsize=(10, 5))
    plt.bar(x - w, overall_vals, width=w, label="overall_score")
    plt.bar(x, draft_vals, width=w, label="draft_quality")
    plt.bar(x + w, cite_vals, width=w, label="citation_coverage")
    plt.xticks(x, labels)
    plt.title(f"Paper Writer Metrics Across Runs ({dataset})")
    plt.ylabel("Score")
    plt.legend()
    plt.tight_layout()
    plt.savefig("metrics_across_runs.png")
    plt.close()
    print("Saved metrics_across_runs.png")


def plot_revision_curves(runs, dataset):
    plt.figure(figsize=(10, 5))
    found = False
    for run in runs:
        results = load_all_results(run)
        keys = [k for k in results.keys() if k.startswith(f"{dataset}_") and k.endswith("_train_log_info")]
        if not keys:
            continue
        found = True
        per_seed_scores = []
        for k in keys:
            seq = results[k]
            per_seed_scores.append([p["score"] for p in seq])
        min_len = min(len(x) for x in per_seed_scores)
        arr = np.array([x[:min_len] for x in per_seed_scores], dtype=float)
        mean_score = arr.mean(axis=0)
        plt.plot(mean_score, label=run)

    if not found:
        print("No train_log_info data found.")
        plt.close()
        return

    plt.title("Revision Progress Across Runs")
    plt.xlabel("Step")
    plt.ylabel("Score")
    plt.legend()
    plt.tight_layout()
    plt.savefig("revision_progress_across_runs.png")
    plt.close()
    print("Saved revision_progress_across_runs.png")


def main():
    runs = sorted_run_dirs()
    if not runs:
        print("No run directories found (expected run_0, run_1, ...).")
        return

    dataset, ds = load_final(runs[0])
    if ds is None:
        print("run_0/final_info.json not found or invalid.")
        return

    plot_main_metrics(runs, dataset)
    plot_revision_curves(runs, dataset)


if __name__ == "__main__":
    main()
