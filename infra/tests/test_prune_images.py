from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

spec = spec_from_file_location("prune_images", Path(__file__).parent.parent / "prune_images.py")
pruner = module_from_spec(spec)
spec.loader.exec_module(pruner)


def run_cleanup(monkeypatch, active, dry_run=False):
    removed = []
    api = "example/prada-api"
    web = "example/prada-web"
    tags = ["sha-" + character * 40 for character in "abcd"]
    ids = ["sha256:" + character * 64 for character in "abcd"]

    def docker(*args):
        if args == ("ps", "-aq", "--no-trunc"):
            return "container\n"
        if args[:2] == ("inspect", "--format"):
            return "\n".join(active(ids)) + "\n"
        if args[:2] == ("image", "ls"):
            repo = args[-1]
            return "\n".join(f"{repo}\t{tag}\t{id_}" for tag, id_ in zip(tags, ids))
        if args[:2] == ("image", "rm"):
            removed.append(args[2])
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(pruner, "docker", docker)
    monkeypatch.setattr(pruner.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout=f"{api}:{tags[0]}\n{web}:{tags[0]}\n"))
    monkeypatch.setattr("sys.argv", ["prune_images.py", *( ["--dry-run"] if dry_run else [])])
    pruner.main()
    return removed, tags, api, web


def test_retains_two_latest_and_any_image_used_by_any_container(monkeypatch):
    removed, tags, api, web = run_cleanup(monkeypatch, lambda ids: [ids[0], ids[2]])
    assert removed == [f"{api}:{tags[3]}", f"{web}:{tags[3]}"]


def test_no_project_container_or_dry_run_never_removes_images(monkeypatch):
    assert run_cleanup(monkeypatch, lambda ids: ["sha256:" + "e" * 64])[0] == []
    assert run_cleanup(monkeypatch, lambda ids: [ids[0]], dry_run=True)[0] == []
