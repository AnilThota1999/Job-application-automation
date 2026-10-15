"""
One command for the whole pipeline:

    python run.py            find jobs -> tailor resumes -> open each application filled in;
                             you review it and press ENTER to submit
    python run.py --auto     same, but safe applications are submitted without asking;
                             the rest are shown to you for review afterwards
    python run.py --hours 72 look further back for jobs

Lever/Workday jobs are saved in manual_apply/ (open manual_apply/index.html).
"""
import os, subprocess, sys

PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))


def step(title, args):
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")
    result = subprocess.run([PY] + args, cwd=HERE)
    if result.returncode != 0:
        print(f"\n'{title}' stopped with an error. Fix it and run again (finished work is kept).")
        sys.exit(result.returncode)


def main():
    hours = []
    if "--hours" in sys.argv:
        hours = ["--hours", sys.argv[sys.argv.index("--hours") + 1]]

    step("STEP 1/3  Finding new jobs", ["step1_discover_jobs.py"] + hours)
    step("STEP 2/3  Tailoring resumes", ["step2_tailor_resume.py"])
    if "--auto" in sys.argv:
        step("STEP 3/3  Auto-applying; anything needing you stays open for review", ["step3_apply.py", "--auto"])
    else:
        step("STEP 3/3  Review each application, press ENTER to submit", ["step3_apply.py"])

    print("\nAll done. Jobs to apply yourself (Lever and other sites): manual_apply/index.html")


if __name__ == "__main__":
    main()
