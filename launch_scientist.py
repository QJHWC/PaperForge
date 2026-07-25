import argparse
import json
import multiprocessing
import os
import os.path as osp
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model

try:
    import torch
except ModuleNotFoundError:
    torch = None

from engine.generate_ideas import check_idea_novelty, generate_ideas
from engine.llm import (
    AVAILABLE_LLMS,
    create_client,
    gateway_profile_env_overrides,
    resolve_gateway_profile,
)
from engine.mvp_workflow import load_baseline_results, validate_template_integrity
from engine.perform_experiments import perform_experiments
from engine.perform_review import load_paper, perform_improvement, perform_review
from engine.perform_writeup import generate_latex, perform_writeup
from engine.run_lock import run_lock
from paperforge.policy import Action, ExecutionPolicy
from paperforge.provider import build_aider_model as build_v3_aider_model
from paperforge.workflow import WorkflowEngine

NUM_REFLECTIONS = 3


def _in_virtualenv() -> bool:
    return bool(getattr(sys, "base_prefix", sys.prefix) != sys.prefix)


def _require_virtualenv(script_name: str) -> None:
    allow_system = os.getenv("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if allow_system or _in_virtualenv():
        return

    raise SystemExit(
        "环境错误: 检测到正在使用系统 Python 运行 PaperForge。\n"
        f"当前解释器: {sys.executable}\n"
        "请先激活虚拟环境后再运行，例如:\n"
        f"  source .venv311/bin/activate && python {script_name} --help\n"
        "如需强制跳过校验，可设置 PAPERFORGE_ALLOW_SYSTEM_PYTHON=1。"
    )


if __name__ == "__main__":
    _require_virtualenv("launch_scientist.py")


@contextmanager
def _results_root_write_lock(experiment: str):
    root = Path("results") / experiment
    root.mkdir(parents=True, exist_ok=True)
    with run_lock(root.resolve(), timeout=30, poll_interval=0.2, verbose=True):
        yield


def print_time():
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _aider_stream_enabled(gateway_profile: str | None = None) -> bool:
    return bool(resolve_gateway_profile(gateway_profile)["stream"])


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run PaperForge experiments")
    parser.add_argument(
        "--skip-idea-generation",
        action="store_true",
        help="Skip idea generation and load existing ideas",
    )
    parser.add_argument(
        "--skip-novelty-check",
        action="store_true",
        help="Skip novelty check and use existing ideas",
    )
    # Template experiment name under templates/
    parser.add_argument(
        "--experiment",
        type=str,
        default="paper_writer",
        help="Experiment to run PaperForge on.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.4-xhigh",
        choices=AVAILABLE_LLMS,
        help="Default model used when stage-specific model flags are not set.",
    )
    parser.add_argument(
        "--idea-model",
        type=str,
        default=None,
        choices=AVAILABLE_LLMS,
        help="Model used for idea generation and novelty check. Defaults to --model.",
    )
    parser.add_argument(
        "--code-model",
        type=str,
        default=None,
        choices=AVAILABLE_LLMS,
        help="Model used for experiment/plot/notes code edits. Defaults to --model.",
    )
    parser.add_argument(
        "--writeup-model",
        type=str,
        default=None,
        choices=AVAILABLE_LLMS,
        help="Model used for paper writeup edits and citation planning. Defaults to --model.",
    )
    parser.add_argument(
        "--review-model",
        type=str,
        default="gpt-5.4-xhigh",
        choices=AVAILABLE_LLMS,
        help="Model used for paper review and improvement review.",
    )
    parser.add_argument(
        "--gateway-profile",
        type=str,
        default=None,
        choices=["safe", "full"],
        help="Gateway profile controlling stream/max_tokens/reasoning defaults.",
    )
    parser.add_argument(
        "--writeup",
        type=str,
        default="latex",
        choices=["latex"],
        help="What format to use for writeup",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="Number of parallel processes to run. 0 for sequential execution.",
    )
    parser.add_argument(
        "--improvement",
        action="store_true",
        help="Improve based on reviews.",
    )
    parser.add_argument(
        "--gpus",
        type=str,
        default=None,
        help="Comma-separated list of GPU IDs to use (e.g., '0,1,2'). If not specified, all available GPUs will be used.",
    )
    parser.add_argument(
        "--num-ideas",
        type=int,
        default=50,
        help="Number of ideas to generate",
    )
    parser.add_argument(
        "--engine",
        type=str,
        default="semanticscholar",
        choices=["semanticscholar", "openalex"],
        help="Scholar engine to use.",
    )
    parser.add_argument(
        "--profile",
        choices=["full"],
        default="full",
        help="Scientist compatibility mode always uses the full execution profile.",
    )
    parser.add_argument(
        "--proposal-id",
        required=True,
        help="Approved proposal stored in the repository workspace database.",
    )
    return parser.parse_args()


def get_available_gpus(gpu_ids=None):
    if gpu_ids is not None:
        return [int(gpu_id.strip()) for gpu_id in gpu_ids.split(",") if gpu_id.strip()]
    if torch is None:
        return []
    try:
        return list(range(torch.cuda.device_count()))
    except Exception:
        return []


def check_latex_dependencies():
    """
    Check if required LaTeX dependencies are installed on the system.
    Returns True if all dependencies are found, False otherwise.
    """
    import shutil
    import sys

    required_dependencies = ['pdflatex', 'bibtex', 'chktex']
    missing_deps = []

    for dep in required_dependencies:
        if shutil.which(dep) is None:
            missing_deps.append(dep)
    
    if missing_deps:
        print(
            "Error: Required LaTeX dependencies not found: "
            + ", ".join(missing_deps),
            file=sys.stderr,
        )
        return False
    
    return True


def build_aider_model(model_name: str, gateway_profile: str | None = None) -> Model:
    return build_v3_aider_model(
        model_name,
        generation_profile=gateway_profile or "safe",
        stage="default",
    )


def resolve_stage_models(args):
    code_model = args.code_model or args.model
    writeup_model = args.writeup_model or args.model
    idea_model = args.idea_model or code_model
    review_model = args.review_model
    return idea_model, code_model, writeup_model, review_model


def _scientist_required_template_files(writeup_format: str) -> list[str]:
    required = [
        "experiment.py",
        "plot.py",
        "seed_ideas.json",
        "prompt.json",
        "baseline/final_info.json",
    ]
    if writeup_format == "latex":
        required.extend(
            [
                "latex/template.tex",
                "latex/ieeecolor.cls",
                "latex/generic.sty",
                "latex/TII.eps",
            ]
        )
    return required


def _resolve_writeup_openai_overrides(model_name: str):
    if model_name != "gpt-5.2-xhigh":
        return {}
    overrides = {}
    writeup_key = os.getenv("OPENAI_WRITEUP_API_KEY", "").strip()
    writeup_base = os.getenv("OPENAI_WRITEUP_BASE_URL", "").strip()
    if writeup_key:
        overrides["OPENAI_API_KEY"] = writeup_key
    if writeup_base:
        overrides["OPENAI_BASE_URL"] = writeup_base
    return overrides


def _apply_env_overrides(overrides: dict):
    backup = {}
    for key, value in overrides.items():
        backup[key] = os.environ.get(key)
        os.environ[key] = value
    return backup


def _restore_env_overrides(backup: dict):
    for key, value in backup.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def worker(
        queue,
        base_dir,
        results_dir,
        code_model,
        writeup_model,
        review_model,
        writeup,
        improvement,
        engine,
        gpu_id,
):
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    writeup_client, writeup_client_model = create_client(writeup_model)
    review_client, review_client_model = create_client(review_model)
    print(f"Worker {gpu_id} started.")
    all_successful = True
    while True:
        idea = queue.get()
        if idea is None:
            break
        success = do_idea(
            base_dir,
            results_dir,
            idea,
            code_model,
            writeup_model,
            writeup_client,
            writeup_client_model,
            review_model,
            review_client,
            review_client_model,
            writeup,
            improvement,
            engine,
            log_file=True,
        )
        all_successful = all_successful and bool(success)
        print(f"Completed idea: {idea['Name']}, Success: {success}")
    print(f"Worker {gpu_id} finished.")
    if not all_successful:
        raise SystemExit(1)


def do_idea(
        base_dir,
        results_dir,
        idea,
        code_model,
        writeup_model,
        writeup_client,
        writeup_client_model,
        review_model,
        review_client,
        review_client_model,
        writeup,
        improvement,
        engine,
        log_file=False,
):
    ## CREATE PROJECT FOLDER
    with _results_root_write_lock(osp.basename(results_dir)):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        idea_name = f"{timestamp}_{idea['Name']}"
        folder_name = osp.join(results_dir, idea_name)
        assert not osp.exists(folder_name), f"Folder {folder_name} already exists."
        destination_dir = folder_name
        shutil.copytree(base_dir, destination_dir, dirs_exist_ok=True)

    with run_lock(Path(folder_name), timeout=30, poll_interval=0.2, verbose=True):
        baseline_results = load_baseline_results(base_dir)
        exp_file = osp.join(folder_name, "experiment.py")
        vis_file = osp.join(folder_name, "plot.py")
        notes = osp.join(folder_name, "notes.txt")
        with open(notes, "w") as f:
            f.write(f"# Title: {idea['Title']}\n")
            f.write(f"# Experiment description: {idea['Experiment']}\n")
            f.write("## Baseline\n")
            f.write(f"Results: {baseline_results}\n")
            f.write("Description: Baseline results.\n")
        if log_file:
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            log_path = osp.join(folder_name, "log.txt")
            log = open(log_path, "a")  # noqa: SIM115 - closed after stream restoration.
            sys.stdout = log
            sys.stderr = log
        try:
            print_time()
            print(f"*Starting idea: {idea_name}*")
            ## PERFORM EXPERIMENTS
            fnames = [exp_file, vis_file, notes]
            io = InputOutput(
                yes=True, chat_history_file=f"{folder_name}/{idea_name}_aider.txt"
            )
            main_model = build_aider_model(code_model)
            coder = Coder.create(
                main_model=main_model,
                fnames=fnames,
                io=io,
                stream=_aider_stream_enabled(),
                use_git=False,
                edit_format="diff",
            )

            print_time()
            print("*Starting Experiments*")
            try:
                success = perform_experiments(idea, folder_name, coder, baseline_results)
            except Exception as e:
                print(f"Error during experiments: {e}")
                print(f"Experiments failed for idea {idea_name}")
                return False

            if not success:
                print(f"Experiments failed for idea {idea_name}")
                return False

            print_time()
            print("*Starting Writeup*")
            ## PERFORM WRITEUP
            if writeup == "latex":
                writeup_file = osp.join(folder_name, "latex", "template.tex")
                # Keep writeup context small to avoid token-limit failures.
                fnames = [writeup_file, notes]
                writeup_io = InputOutput(
                    yes=True, chat_history_file=f"{folder_name}/{idea_name}_writeup_aider.txt"
                )
                writeup_overrides = _resolve_writeup_openai_overrides(writeup_model)
                writeup_backup = _apply_env_overrides(writeup_overrides) if writeup_overrides else {}
                if writeup_overrides:
                    print(
                        "Writeup OpenAI routing enabled for "
                        f"{writeup_model}: {os.getenv('OPENAI_BASE_URL')}"
                    )
                try:
                    main_model = build_aider_model(writeup_model)
                    coder = Coder.create(
                        main_model=main_model,
                        fnames=fnames,
                        io=writeup_io,
                        stream=_aider_stream_enabled(),
                        use_git=False,
                        edit_format="diff",
                    )
                    perform_writeup(
                        idea,
                        folder_name,
                        coder,
                        writeup_client,
                        writeup_client_model,
                        engine=engine,
                    )
                except Exception as e:
                    print(f"Failed to perform writeup: {e}")
                    return False
                finally:
                    if writeup_overrides:
                        _restore_env_overrides(writeup_backup)
                print("Done writeup")
            else:
                raise ValueError(f"Writeup format {writeup} not supported.")

            print_time()
            print("*Starting Review*")
            ## REVIEW PAPER
            if writeup == "latex":
                try:
                    paper_text = load_paper(f"{folder_name}/{idea['Name']}.pdf")
                    review = perform_review(
                        paper_text,
                        model=review_client_model,
                        client=review_client,
                        num_reflections=5,
                        num_fs_examples=1,
                        num_reviews_ensemble=5,
                        temperature=0.1,
                    )
                    # Store the review in separate review.txt file
                    with open(osp.join(folder_name, "review.txt"), "w") as f:
                        f.write(json.dumps(review, indent=4))
                except Exception as e:
                    print(f"Failed to perform review: {e}")
                    return False

            ## IMPROVE WRITEUP
            if writeup == "latex" and improvement:
                print_time()
                print("*Starting Improvement*")
                try:
                    perform_improvement(review, coder)
                    generate_latex(
                        coder, folder_name, f"{folder_name}/{idea['Name']}_improved.pdf"
                    )
                    paper_text = load_paper(f"{folder_name}/{idea['Name']}_improved.pdf")
                    review = perform_review(
                        paper_text,
                        model=review_client_model,
                        client=review_client,
                        num_reflections=5,
                        num_fs_examples=1,
                        num_reviews_ensemble=5,
                        temperature=0.1,
                    )
                    # Store the review in separate review.txt file
                    with open(osp.join(folder_name, "review_improved.txt"), "w") as f:
                        f.write(json.dumps(review))
                except Exception as e:
                    print(f"Failed to perform improvement: {e}")
                    return False
            return True
        except Exception as e:
            print(f"Failed to evaluate idea {idea_name}: {str(e)}")
            return False
        finally:
            print("FINISHED IDEA")
            if log_file:
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                log.close()


if __name__ == "__main__":
    args = parse_arguments()
    execution_policy = ExecutionPolicy.from_value(args.profile)
    execution_policy.require(Action.CODE_PATCH)
    execution_policy.require(Action.EXPERIMENT_FULL)
    execution_policy.require(Action.LOCAL_EXECUTE)
    WorkflowEngine(Path.cwd()).require_approval(args.proposal_id)
    for key, value in gateway_profile_env_overrides(args.gateway_profile).items():
        os.environ[key] = value

    try:
        base_dir = validate_template_integrity(
            args.experiment,
            required_files=_scientist_required_template_files(args.writeup),
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if torch is None and args.gpus is None:
        print(
            "Warning: `torch` is not installed, GPU auto-detection is disabled. "
            "Proceeding with CPU/sequential mode unless --gpus is provided."
        )

    # Check available GPUs and adjust parallel processes if necessary
    available_gpus = get_available_gpus(args.gpus)
    if args.parallel > len(available_gpus):
        print(
            f"Warning: Requested {args.parallel} parallel processes, but only {len(available_gpus)} GPUs available. Adjusting to {len(available_gpus)}."
        )
        args.parallel = len(available_gpus)

    print(f"Using GPUs: {available_gpus}")

    # Check LaTeX dependencies before proceeding
    if args.writeup == "latex" and not check_latex_dependencies():
        sys.exit(1)

    idea_model, code_model, writeup_model, review_model = resolve_stage_models(args)
    print(
        f"Using models -> idea: {idea_model}, code: {code_model}, "
        f"writeup: {writeup_model}, review: {review_model}"
    )

    idea_client, idea_client_model = create_client(idea_model)
    writeup_client, writeup_client_model = create_client(writeup_model)
    review_client, review_client_model = create_client(review_model)

    results_dir = osp.join("results", args.experiment)
    with run_lock(Path(base_dir), timeout=30, poll_interval=0.2, verbose=True):
        ideas = generate_ideas(
            base_dir,
            client=idea_client,
            model=idea_client_model,
            skip_generation=args.skip_idea_generation,
            max_num_generations=args.num_ideas,
            num_reflections=NUM_REFLECTIONS,
        )
        if not args.skip_novelty_check:
            ideas = check_idea_novelty(
                ideas,
                base_dir=base_dir,
                client=idea_client,
                model=idea_client_model,
                engine=args.engine,
            )

        with open(osp.join(base_dir, "ideas.json"), "w") as f:
            json.dump(ideas, f, indent=4)

    # Backward compatibility: older seed ideas may not include "novel".
    # When novelty check is skipped, treat missing flags as novel.
    if args.skip_novelty_check:
        novel_ideas = [idea for idea in ideas if idea.get("novel", True)]
    else:
        novel_ideas = [idea for idea in ideas if idea.get("novel", False)]
    # novel_ideas = list(reversed(novel_ideas))

    all_successful = bool(novel_ideas)
    if args.parallel > 0:
        print(f"Running {args.parallel} parallel processes")
        queue = multiprocessing.Queue()
        for idea in novel_ideas:
            queue.put(idea)

        processes = []
        for i in range(args.parallel):
            gpu_id = available_gpus[i % len(available_gpus)]
            p = multiprocessing.Process(
                target=worker,
                args=(
                    queue,
                    base_dir,
                    results_dir,
                    code_model,
                    writeup_model,
                    review_model,
                    args.writeup,
                    args.improvement,
                    args.engine,
                    gpu_id,
                ),
            )
            p.start()
            time.sleep(150)
            processes.append(p)

        # Signal workers to exit
        for _ in range(args.parallel):
            queue.put(None)

        for p in processes:
            p.join()
            all_successful = all_successful and p.exitcode == 0

        print("All parallel processes completed.")
    else:
        for idea in novel_ideas:
            print(f"Processing idea: {idea['Name']}")
            try:
                success = do_idea(
                    base_dir,
                    results_dir,
                    idea,
                    code_model,
                    writeup_model,
                    writeup_client,
                    writeup_client_model,
                    review_model,
                    review_client,
                    review_client_model,
                    args.writeup,
                    args.improvement,
                    args.engine,
                )
                all_successful = all_successful and bool(success)
                print(f"Completed idea: {idea['Name']}, Success: {success}")
            except Exception as e:
                all_successful = False
                print(f"Failed to evaluate idea {idea['Name']}: {str(e)}")
                import traceback
                print(traceback.format_exc())
    print("All ideas evaluated.")
    if not all_successful:
        raise SystemExit(1)
