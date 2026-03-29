import argparse
import json
import os
import random
import time

import numpy as np


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def _bounded(value: float, low: float, high: float) -> float:
    return float(max(low, min(high, value)))


def simulate_run(config: dict):
    """
    Lightweight simulation for a paper-writing workflow.
    Produces deterministic metrics so MVP loops can run without domain datasets.
    """
    seed = int(config["seed"])
    set_seed(seed)

    rounds = int(config["rounds"])
    steps_per_round = int(config["steps_per_round"])

    structure_depth = float(config["structure_depth"])
    evidence_ratio = float(config["evidence_ratio"])
    revision_rounds = int(config["revision_rounds"])
    citation_density = float(config["citation_density"])

    draft_quality = _bounded(
        0.62
        + 0.10 * structure_depth
        + 0.08 * evidence_ratio
        + 0.04 * revision_rounds
        + np.random.normal(0.0, 0.01),
        0.35,
        0.99,
    )
    citation_coverage = _bounded(
        0.55 + 0.18 * citation_density + 0.12 * evidence_ratio + np.random.normal(0.0, 0.01),
        0.20,
        0.99,
    )
    coherence = _bounded(
        0.58 + 0.16 * structure_depth + 0.08 * revision_rounds + np.random.normal(0.0, 0.01),
        0.25,
        0.99,
    )
    readability = _bounded(
        0.60 + 0.11 * structure_depth - 0.05 * max(0.0, citation_density - 1.2) + np.random.normal(0.0, 0.01),
        0.25,
        0.99,
    )
    revision_efficiency = _bounded(
        0.72 - 0.06 * max(0, revision_rounds - 3) + 0.05 * evidence_ratio + np.random.normal(0.0, 0.01),
        0.25,
        0.99,
    )

    overall_score = _bounded(
        0.30 * draft_quality
        + 0.20 * citation_coverage
        + 0.20 * coherence
        + 0.20 * readability
        + 0.10 * revision_efficiency,
        0.0,
        1.0,
    )

    train_log_info = []
    val_log_info = []
    base_loss = 1.5 - overall_score
    for r in range(rounds):
        for step in range(steps_per_round):
            progress = (r * steps_per_round + step + 1) / float(rounds * steps_per_round)
            loss = _bounded(base_loss * (1.0 - 0.70 * progress) + np.random.normal(0.0, 0.01), 0.02, 10.0)
            score = _bounded(100.0 * (0.30 + 0.65 * progress * overall_score) + np.random.normal(0.0, 0.7), 0.0, 100.0)
            train_log_info.append({"round": r, "step": step, "loss": float(loss), "score": float(score)})

        val_loss = _bounded(base_loss * (1.0 - 0.60 * (r + 1) / rounds) + np.random.normal(0.0, 0.01), 0.02, 10.0)
        val_score = _bounded(100.0 * (0.35 + 0.55 * (r + 1) / rounds * overall_score) + np.random.normal(0.0, 0.6), 0.0, 100.0)
        val_log_info.append({"round": r, "loss": float(val_loss), "score": float(val_score)})

    final_info = {
        "draft_quality": draft_quality,
        "citation_coverage": citation_coverage,
        "coherence": coherence,
        "readability": readability,
        "revision_efficiency": revision_efficiency,
        "overall_score": overall_score,
        "config": config,
    }
    return train_log_info, val_log_info, final_info


def main():
    parser = argparse.ArgumentParser(description="Paper-writer workflow simulation experiment")
    parser.add_argument("--out_dir", type=str, default="run_0", help="Output directory")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    print(f"Outputs will be saved to {args.out_dir}")

    dataset_name = "paper_writing_pipeline"
    seeds = [0, 1]

    base_config = {
        "workspace_mode": "paper_writer",
        "structure_depth": 1.0,
        "evidence_ratio": 1.0,
        "revision_rounds": 3,
        "citation_density": 1.0,
        "rounds": 6,
        "steps_per_round": 8,
    }

    all_results = {}
    final_info_list = []

    for seed in seeds:
        cfg = dict(base_config)
        cfg["seed"] = seed
        cfg["out_dir"] = args.out_dir

        start = time.time()
        train_log_info, val_log_info, final_info = simulate_run(cfg)
        total_time = time.time() - start
        final_info["total_train_time"] = float(total_time)
        final_info_list.append(final_info)

        key_prefix = f"{dataset_name}_{seed}"
        all_results[f"{key_prefix}_final_info"] = final_info
        all_results[f"{key_prefix}_train_log_info"] = train_log_info
        all_results[f"{key_prefix}_val_log_info"] = val_log_info
        print(
            f"Seed {seed}: overall_score={final_info['overall_score']:.4f}, "
            f"draft_quality={final_info['draft_quality']:.4f}, citation_coverage={final_info['citation_coverage']:.4f}"
        )

    final_info_dict = {k: [d[k] for d in final_info_list if k in d] for k in final_info_list[0].keys()}
    means = {f"{k}_mean": float(np.mean(v)) for k, v in final_info_dict.items() if isinstance(v[0], (int, float))}
    stderrs = {f"{k}_stderr": float(np.std(v) / np.sqrt(len(v))) for k, v in final_info_dict.items() if isinstance(v[0], (int, float))}

    final_infos = {
        dataset_name: {
            "means": means,
            "stderrs": stderrs,
            "final_info_dict": final_info_dict,
        }
    }

    with open(os.path.join(args.out_dir, "final_info.json"), "w", encoding="utf-8") as f:
        json.dump(final_infos, f, indent=2)
    with open(os.path.join(args.out_dir, "all_results.npy"), "wb") as f:
        np.save(f, all_results)

    print(f"All results saved to {args.out_dir}")


if __name__ == "__main__":
    main()
