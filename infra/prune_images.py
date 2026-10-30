#!/usr/bin/env python3
"""Remove old Prada Sound SHA-tagged images, preserving running images and a rollback."""
import argparse
import re
import subprocess
from pathlib import Path


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], check=True, text=True, capture_output=True)
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.project_root.resolve()
    # Compose selects only this project's API and web images; never touch other repositories.
    result = subprocess.run(["docker", "compose", "config", "--images", "api", "web"],
                            check=True, capture_output=True, text=True, cwd=root)
    repositories = {ref.rsplit(":", 1)[0] for ref in result.stdout.splitlines() if ":" in ref}
    container_ids = docker("ps", "-aq", "--no-trunc").splitlines()
    if not repositories or not container_ids:
        print("No project images or containers; nothing removed.")
        return
    active = set(docker("inspect", "--format", "{{.Image}}", *container_ids).splitlines())
    failures = 0
    for repository in sorted(repositories):
        lines = docker("image", "ls", "--no-trunc", "--format",
                       "{{.Repository}}\t{{.Tag}}\t{{.ID}}", repository).splitlines()
        images = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == repository and re.fullmatch(r"sha-[0-9a-f]{40}", parts[1]):
                images.append(parts)
        if not any(image_id in active for _, _, image_id in images):
            print(f"{repository}: no active SHA image; skipping for safety")
            continue
        # Docker lists newest first. Retain two newest and every image used by any container.
        for _, tag, image_id in images[2:]:
            if image_id in active:
                continue
            reference = f"{repository}:{tag}"
            if args.dry_run:
                print(f"Would remove {reference}")
                continue
            try:
                docker("image", "rm", reference)
                print(f"Removed {reference}")
            except subprocess.CalledProcessError:
                failures += 1
                print(f"Could not remove {reference}; left it in place")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
