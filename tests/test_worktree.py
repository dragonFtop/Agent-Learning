import shutil
import subprocess

import pytest

from agent.permissions import PermissionPolicy
from agent.runtime import AgentRuntime
from agent.tasks import TaskStore
from agent.teams import TeamManager
from agent.worktree import WorktreeManager, validate_worktree_name

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is required for worktree tests")


def _run(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _repo(tmp_path):
    _run(tmp_path, "init")
    _run(tmp_path, "config", "user.email", "tests@example.com")
    _run(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-m", "initial")
    return tmp_path


def _store(tmp_path):
    return TaskStore(tmp_path / ".tasks", workspace=tmp_path)


def test_worktree_name_validation():
    assert validate_worktree_name("feature-1") is None
    assert validate_worktree_name("a.b_c") is None
    for bad in ("", ".", "..", "../escape", "a/b", "-leading", "a" * 65, "a..b"):
        assert validate_worktree_name(bad) is not None


def test_create_binds_task_and_resolves_cwd(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    manager = WorktreeManager(repo, tasks)
    task = tasks.create("feature")

    message = manager.create("feat-1", task.id)

    assert "created" in message
    path = repo / ".worktrees" / "feat-1"
    assert path.is_dir()
    assert (path / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert tasks.load(task.id).worktree == "feat-1"
    resolved, error = manager.task_cwd(tasks.load(task.id))
    assert error is None
    assert resolved.resolve() == path.resolve()
    assert "wt/feat-1" in _run(repo, "branch", "--list", "wt/feat-1")


def test_create_requires_pending_unowned_task(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    manager = WorktreeManager(repo, tasks)
    task = tasks.create("feature")
    tasks.claim(task.id, owner="worker")

    message = manager.create("feat-1", task.id)

    assert "pending and unowned" in message
    assert not (repo / ".worktrees" / "feat-1").exists()


def test_create_rejects_bound_name_and_missing_task(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    manager = WorktreeManager(repo, tasks)
    first = tasks.create("first")
    second = tasks.create("second")
    assert "created" in manager.create("shared", first.id)

    assert "already bound" in manager.create("shared", second.id)
    assert "not usable" in manager.create("other", "task_deadbeef")


def test_create_requires_git_root(tmp_path):
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        pytest.skip("temporary directory is inside a Git repository")
    tasks = _store(tmp_path)
    manager = WorktreeManager(tmp_path, tasks)
    task = tasks.create("feature")

    message = manager.create("feat-1", task.id)

    assert "Git repository" in message


def test_remove_refuses_active_dirty_and_retains_branch(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    manager = WorktreeManager(repo, tasks)
    task = tasks.create("feature")
    manager.create("feat-1", task.id)
    path = repo / ".worktrees" / "feat-1"

    tasks.claim(task.id, owner="worker")
    assert "active task" in manager.remove("feat-1")

    tasks.complete(task.id, owner="worker")
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    assert "uncommitted" in manager.remove("feat-1")

    message = manager.remove("feat-1", discard_changes=True)
    assert "retained" in message
    assert not path.exists()
    assert tasks.load(task.id).worktree is None
    assert "wt/feat-1" in _run(repo, "show-ref", "--verify", "refs/heads/wt/feat-1")


def test_broken_binding_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    manager = WorktreeManager(repo, tasks)
    task = tasks.create("feature")
    manager.create("feat-1", task.id)
    tasks.set_worktree(task.id, "ghost")

    resolved, error = manager.task_cwd(tasks.load(task.id))

    assert resolved is None
    assert "not registered" in error


def test_teammate_assignment_uses_worktree_cwd(tmp_path):
    repo = _repo(tmp_path)
    tasks = _store(repo)
    worktrees = WorktreeManager(repo, tasks)
    task = tasks.create("feature")
    assert "created" in worktrees.create("feat-1", task.id)
    runtime = AgentRuntime(
        client=object(),
        model="fake-model",
        workdir=repo,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=repo),
    )
    runtime.tasks = tasks
    runtime.worktrees = worktrees
    teams = TeamManager(runtime, tasks=tasks, worktrees=worktrees, idle_interval=0.02)
    runtime.teams = teams

    message = teams.spawn("worker", "engineer", "work", task_id=task.id, start=False)

    assert "spawned" in message
    record = teams.record("worker")
    assert record.cwd.resolve() == (repo / ".worktrees" / "feat-1").resolve()
    assert record.runtime.runtime.task_id == task.id
    assert teams.gate_check("worker", "write_file") is None
    assert teams.worktree_users("feat-1") == ["worker"]
    assert "still in use" in teams.remove_worktree("feat-1")
