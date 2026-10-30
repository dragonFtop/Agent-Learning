"""Built-in tool specs (s02).

Adding a tool means registering one more ``ToolSpec`` here (or in a feature
module); the agent loop never changes.
"""

from __future__ import annotations

from tools.bash import run_bash
from tools.file_ops import run_edit, run_glob, run_read, run_write
from tools.registry import ToolContext, ToolRegistry, ToolSpec

_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}


def _bash(args: dict, ctx: ToolContext) -> str:
    command = str(args.get("command", ""))
    if args.get("run_in_background"):
        background = getattr(ctx.runtime, "background", None)
        if background is None:
            return "Error: background tasks are not available in this session"
        return background.start(command, ctx)
    return run_bash(command, cwd=ctx.cwd)


def _read(args: dict, ctx: ToolContext) -> str:
    return run_read(
        str(args.get("path", "")),
        args.get("limit"),
        int(args.get("offset", 0) or 0),
        cwd=ctx.cwd,
    )


def _write(args: dict, ctx: ToolContext) -> str:
    return run_write(str(args.get("path", "")), str(args.get("content", "")), cwd=ctx.cwd)


def _edit(args: dict, ctx: ToolContext) -> str:
    return run_edit(
        str(args.get("path", "")),
        str(args.get("old_text", "")),
        str(args.get("new_text", "")),
        cwd=ctx.cwd,
    )


def _glob(args: dict, ctx: ToolContext) -> str:
    return run_glob(str(args.get("pattern", "")), cwd=ctx.cwd)


def _load_skill(args: dict, ctx: ToolContext) -> str:
    loader = getattr(ctx.runtime, "skill_loader", None)
    if loader is None:
        from config import SKILL_LOADER

        loader = SKILL_LOADER
    return loader.get_content(str(args.get("name", "")))


def _compact(args: dict, ctx: ToolContext) -> str:
    runtime = ctx.runtime
    if runtime is None or getattr(runtime, "context_manager", None) is None:
        return "Error: context compaction is not available"
    return runtime.force_compact(str(args.get("focus", "")))


def _task(args: dict, ctx: ToolContext) -> str:
    runtime = ctx.runtime
    if runtime is None:
        return "Error: subagents are not available"
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return "Error: prompt is required"
    description = str(args.get("description", "subtask"))
    return runtime.spawn_subagent(prompt, description=description)


def _todo_write(args: dict, ctx: ToolContext) -> str:
    todos = getattr(ctx.runtime, "todos", None)
    if todos is None:
        return "Error: the todo list is not available in this context"
    try:
        return todos.update(args.get("todos", []))
    except ValueError as error:
        return f"Error: {error}"


def _task_store(ctx: ToolContext):
    store = getattr(ctx.runtime, "tasks", None)
    if store is None:
        return None, "Error: the task board is not available in this context"
    return store, None


def _create_task(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    task = store.create(str(args.get("subject", "")), str(args.get("description", "")))
    return f"Created {task.id}: {task.subject}"


def _update_task(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    add_blocked_by = args.get("addBlockedBy", [])
    if isinstance(add_blocked_by, str):
        add_blocked_by = [item.strip() for item in add_blocked_by.split(",") if item.strip()]
    if not isinstance(add_blocked_by, list) or not add_blocked_by:
        return "Error: addBlockedBy must be a non-empty list of task ids"
    task = store.update_dependencies(str(args.get("task_id", "")), add_blocked_by)
    dependencies = ", ".join(task.blockedBy) or "(none)"
    return f"Updated {task.id} blockedBy: {dependencies}"


def _list_tasks(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    return store.render_list()


def _get_task(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    return store.get_json(str(args.get("task_id", "")))


def _claim_task(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    task_id = str(args.get("task_id", ""))
    message = store.claim(task_id, owner=ctx.agent_name)
    if message.startswith("Claimed ") and ctx.profile == "teammate":
        teams = getattr(ctx.runtime, "teams", None) if ctx.runtime else None
        if teams is not None:
            error = teams.after_claim(ctx.agent_name, task_id)
            if error:
                try:
                    store.release(task_id, owner=ctx.agent_name)
                except (OSError, ValueError, TypeError):
                    pass
                return error
    return message


def _complete_task(args: dict, ctx: ToolContext) -> str:
    store, error = _task_store(ctx)
    if error:
        return error
    return store.complete(str(args.get("task_id", "")), owner=ctx.agent_name)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _schedule_cron(args: dict, ctx: ToolContext) -> str:
    scheduler = getattr(ctx.runtime, "cron", None)
    if scheduler is None:
        return "Error: the cron scheduler is not available in this context"
    job = scheduler.schedule(
        str(args.get("cron", "")),
        str(args.get("prompt", "")),
        recurring=_as_bool(args.get("recurring"), True),
        durable=_as_bool(args.get("durable"), True),
    )
    if isinstance(job, str):
        return f"Error: {job}"
    return f"Scheduled {job.id}: {job.cron} -> {job.prompt}"


def _list_crons(args: dict, ctx: ToolContext) -> str:
    scheduler = getattr(ctx.runtime, "cron", None)
    if scheduler is None:
        return "Error: the cron scheduler is not available in this context"
    return scheduler.list_text()


def _cancel_cron(args: dict, ctx: ToolContext) -> str:
    scheduler = getattr(ctx.runtime, "cron", None)
    if scheduler is None:
        return "Error: the cron scheduler is not available in this context"
    return scheduler.cancel(str(args.get("job_id", "")))


def _teams(ctx: ToolContext):
    teams = getattr(ctx.runtime, "teams", None) if ctx.runtime else None
    if teams is None:
        return None, "Error: agent teams are not available in this context"
    return teams, None


def _spawn_teammate(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.spawn(
        str(args.get("name", "")),
        str(args.get("role", "")),
        str(args.get("prompt", "")),
        task_id=str(args.get("task_id", "")).strip() or None,
        require_plan=_as_bool(args.get("require_plan"), False),
    )


def _list_teammates(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.list_text()


def _send_message(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    to = str(args.get("to", "")).strip()
    content = str(args.get("content", ""))
    if not to:
        return "Error: 'to' is required"
    if not content.strip():
        return "Error: message content cannot be empty"
    if ctx.profile == "teammate":
        return teams.send_from(ctx.agent_name, to, content)
    return teams.send_message(to, content)


def _submit_plan(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.submit_plan(ctx.agent_name, str(args.get("plan", "")))


def _request_shutdown(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.request_shutdown(str(args.get("teammate", "")).strip())


def _request_plan(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.request_plan(
        str(args.get("teammate", "")).strip(),
        str(args.get("task", "")),
    )


def _review_plan(args: dict, ctx: ToolContext) -> str:
    teams, error = _teams(ctx)
    if error:
        return error
    return teams.review_plan(
        str(args.get("request_id", "")).strip(),
        _as_bool(args.get("approve"), False),
        str(args.get("feedback", "")),
    )


def _create_worktree(args: dict, ctx: ToolContext) -> str:
    worktrees = getattr(ctx.runtime, "worktrees", None) if ctx.runtime else None
    if worktrees is None:
        return "Error: worktrees are not available in this context"
    return worktrees.create(
        str(args.get("name", "")).strip(),
        str(args.get("task_id", "")).strip(),
    )


def _remove_worktree(args: dict, ctx: ToolContext) -> str:
    worktrees = getattr(ctx.runtime, "worktrees", None) if ctx.runtime else None
    if worktrees is None:
        return "Error: worktrees are not available in this context"
    name = str(args.get("name", "")).strip()
    discard = _as_bool(args.get("discard_changes"), False)
    teams = getattr(ctx.runtime, "teams", None) if ctx.runtime else None
    if teams is not None:
        return teams.remove_worktree(name, discard_changes=discard)
    return worktrees.remove(name, discard_changes=discard)


def _connect_mcp(args: dict, ctx: ToolContext) -> str:
    mcp = getattr(ctx.runtime, "mcp", None) if ctx.runtime else None
    if mcp is None:
        return "Error: MCP is not available in this context"
    return mcp.connect(str(args.get("name", "")).strip())


def _list_mcp_tools(args: dict, ctx: ToolContext) -> str:
    mcp = getattr(ctx.runtime, "mcp", None) if ctx.runtime else None
    if mcp is None:
        return "Error: MCP is not available in this context"
    return mcp.list_tools_text()


def _run_workflow(args: dict, ctx: ToolContext) -> str:
    workflow = getattr(ctx.runtime, "workflow", None) if ctx.runtime else None
    if workflow is None:
        return "Error: workflows are not available in this context"
    run_args = args.get("args")
    if run_args is not None and not isinstance(run_args, dict):
        return "Error: args must be an object"
    return workflow.run(
        str(args.get("name", "")).strip(),
        run_args,
        resume_from_run_id=str(args.get("resume_from_run_id", "")).strip() or None,
    )


def _list_workflows(args: dict, ctx: ToolContext) -> str:
    workflow = getattr(ctx.runtime, "workflow", None) if ctx.runtime else None
    if workflow is None:
        return "Error: workflows are not available in this context"
    return workflow.list_text()


BASE_TOOLS = [
    ToolSpec(
        name="bash",
        description=(
            "Run a shell command in the workspace and return its output plus exit code. "
            "Use this for builds, tests, git and short-lived commands. Do not use it to read or "
            "edit files when read_file/write_file/edit_file can do the job. Set "
            "run_in_background=true only for independent long-running commands; the result will "
            "arrive as a notification in a later turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {**_STRING, "description": "Command line to execute."},
                "run_in_background": {
                    "type": "boolean",
                    "description": "Run asynchronously and notify when finished.",
                },
            },
            "required": ["command"],
        },
        handler=_bash,
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read a UTF-8 text file inside the workspace. Output is capped; use offset and limit "
            "to page through large files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {**_STRING, "description": "Workspace-relative path."},
                "limit": {**_INTEGER, "description": "Maximum number of lines to return."},
                "offset": {**_INTEGER, "description": "Zero-based first line."},
            },
            "required": ["path"],
        },
        handler=_read,
    ),
    ToolSpec(
        name="write_file",
        description=(
            "Create or overwrite a UTF-8 text file inside the workspace. Parent directories are "
            "created automatically. Prefer edit_file for small changes to existing files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {**_STRING, "description": "Workspace-relative path."},
                "content": {**_STRING, "description": "Complete file content."},
            },
            "required": ["path", "content"],
        },
        handler=_write,
    ),
    ToolSpec(
        name="edit_file",
        description=(
            "Replace the first exact occurrence of old_text with new_text. old_text must match "
            "the file exactly, including whitespace. Read the file first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {**_STRING, "description": "Workspace-relative path."},
                "old_text": {**_STRING, "description": "Exact text to replace."},
                "new_text": {**_STRING, "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
        handler=_edit,
    ),
    ToolSpec(
        name="glob",
        description="Find files by glob pattern inside the workspace. '**' matches recursively.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {**_STRING, "description": "Glob pattern, e.g. '**/*.py'."},
            },
            "required": ["pattern"],
        },
        handler=_glob,
    ),
    ToolSpec(
        name="load_skill",
        description="Load the full instructions of a named skill. Call this before unfamiliar tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "Skill name from the catalog."},
            },
            "required": ["name"],
        },
        handler=_load_skill,
    ),
    ToolSpec(
        name="compact",
        description=(
            "Manually compress conversation history when it is getting long. Preserve the focus "
            "described in the argument. Prefer finishing the current step first."
        ),
        input_schema={
            "type": "object",
            "properties": {"focus": {**_STRING, "description": "What to preserve in the summary."}},
        },
        handler=_compact,
        profiles=("lead",),
    ),
    ToolSpec(
        name="task",
        description=(
            "Delegate an isolated subtask to a subagent. The subagent starts with a fresh context, "
            "has no access to this conversation, and returns only its final report. Use it for "
            "focused research or independent verification. It cannot ask the user questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {**_STRING, "description": "Complete, self-contained task description."},
                "description": {**_STRING, "description": "Short label shown in the transcript."},
            },
            "required": ["prompt"],
        },
        handler=_task,
        profiles=("lead",),
    ),
    ToolSpec(
        name="todo_write",
        description=(
            "Create or replace the current session's todo list. Use it for multi-step work: mark "
            "exactly one item in_progress, update statuses as you go, and mark items completed "
            "only after verifying the result. This list disappears when the session ends."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {**_STRING, "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
        handler=_todo_write,
        profiles=("lead",),
    ),
    ToolSpec(
        name="create_task",
        description=(
            "Create a durable task on the shared board and return its runtime-generated ID. "
            "Create all task nodes first, then add dependencies with update_task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {**_STRING, "description": "Short imperative task title."},
                "description": {**_STRING, "description": "Optional details."},
            },
            "required": ["subject"],
        },
        handler=_create_task,
        profiles=("lead",),
    ),
    ToolSpec(
        name="update_task",
        description=(
            "Add dependency edges to a pending, unowned task using IDs returned by create_task. "
            "A task can only be claimed after every listed dependency is completed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {**_STRING, "description": "Task ID such as task_1a2b3c4d."},
                "addBlockedBy": {
                    "type": "array",
                    "items": _STRING,
                    "minItems": 1,
                    "description": "IDs of tasks that must complete first.",
                },
            },
            "required": ["task_id", "addBlockedBy"],
        },
        handler=_update_task,
        profiles=("lead",),
    ),
    ToolSpec(
        name="list_tasks",
        description="List every task with status, owner, dependencies and worktree.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_tasks,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="get_task",
        description="Read one task record as JSON.",
        input_schema={
            "type": "object",
            "properties": {"task_id": _STRING},
            "required": ["task_id"],
        },
        handler=_get_task,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="claim_task",
        description=(
            "Claim a pending task whose dependencies are all completed. The task becomes "
            "in_progress and is owned by this agent."
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": _STRING},
            "required": ["task_id"],
        },
        handler=_claim_task,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="complete_task",
        description=(
            "Mark a task completed. Only the owning agent can complete it, and only after "
            "verifying the work."
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": _STRING},
            "required": ["task_id"],
        },
        handler=_complete_task,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="schedule_cron",
        description=(
            "Schedule a prompt with a 5-field local-time cron expression "
            "(minute hour day-of-month month day-of-week). Due jobs run as future turns."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "cron": {**_STRING, "description": "Example: '0 9 * * 1-5'."},
                "prompt": {**_STRING, "description": "Instruction for the scheduled turn."},
                "recurring": {"type": "boolean", "description": "Default true."},
                "durable": {
                    "type": "boolean",
                    "description": "Persist across restarts. Default true.",
                },
            },
            "required": ["cron", "prompt"],
        },
        handler=_schedule_cron,
        profiles=("lead",),
    ),
    ToolSpec(
        name="list_crons",
        description="List scheduled cron jobs and their next-run state.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_crons,
        profiles=("lead",),
    ),
    ToolSpec(
        name="cancel_cron",
        description="Cancel a cron job by ID.",
        input_schema={
            "type": "object",
            "properties": {"job_id": _STRING},
            "required": ["job_id"],
        },
        handler=_cancel_cron,
        profiles=("lead",),
    ),
    ToolSpec(
        name="spawn_teammate",
        description=(
            "Spawn a persistent teammate with its own context and working directory. Give it a "
            "clear role and a self-contained prompt. Pass task_id to claim and assign an existing "
            "pending task. Set require_plan=true when the teammate must get approval before "
            "changing files. After spawning, end your turn instead of polling; team events wake you."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "1-64 letters, digits, '_' or '-'."},
                "role": {**_STRING, "description": "Short responsibility, e.g. 'test engineer'."},
                "prompt": {**_STRING, "description": "Complete instructions for the teammate."},
                "task_id": {**_STRING, "description": "Optional task to claim for this teammate."},
                "require_plan": {
                    "type": "boolean",
                    "description": "Require plan approval before bash or file changes.",
                },
            },
            "required": ["name", "role", "prompt"],
        },
        handler=_spawn_teammate,
        profiles=("lead",),
    ),
    ToolSpec(
        name="list_teammates",
        description="List active teammates with status, assignment and plan gate.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_teammates,
        profiles=("lead",),
    ),
    ToolSpec(
        name="send_message",
        description=(
            "Send a message to the lead or an active teammate. Messages are delivered as team "
            "events in the recipient's next turn. Use it for coordination, not for results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {**_STRING, "description": "'lead' or a teammate name."},
                "content": {**_STRING, "description": "Message text."},
            },
            "required": ["to", "content"],
        },
        handler=_send_message,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="submit_plan",
        description=(
            "Submit a work plan to the lead for approval. While a plan is pending, bash and "
            "file writes are blocked. When a plan is rejected, revise it and submit again."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan": {**_STRING, "description": "Concrete steps and files you will touch."},
            },
            "required": ["plan"],
        },
        handler=_submit_plan,
        profiles=("teammate",),
    ),
    ToolSpec(
        name="request_shutdown",
        description=(
            "Ask a teammate to finish its current step and stop. The teammate acknowledges with "
            "a shutdown response and releases any unfinished task."
        ),
        input_schema={
            "type": "object",
            "properties": {"teammate": {**_STRING}},
            "required": ["teammate"],
        },
        handler=_request_shutdown,
        profiles=("lead",),
    ),
    ToolSpec(
        name="request_plan",
        description="Require a teammate to submit a plan before changing the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "teammate": {**_STRING},
                "task": {**_STRING, "description": "What the plan should cover."},
            },
            "required": ["teammate", "task"],
        },
        handler=_request_plan,
        profiles=("lead",),
    ),
    ToolSpec(
        name="review_plan",
        description=(
            "Approve or reject a plan request. Use the request_id from the plan_approval_request "
            "team event. Approval applies only to the teammate's current task assignment."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "request_id": {**_STRING, "description": "Example: req_1a2b3c."},
                "approve": {"type": "boolean"},
                "feedback": {**_STRING, "description": "Optional explanation."},
            },
            "required": ["request_id", "approve"],
        },
        handler=_review_plan,
        profiles=("lead",),
    ),
    ToolSpec(
        name="create_worktree",
        description=(
            "Create a Git worktree under .worktrees and bind it to a pending, unowned task. "
            "Teammates assigned that task work in the worktree on branch wt/<name>. "
            "Only create one when parallel edits would otherwise conflict."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "1-64 letters, digits, '.', '_' or '-'."},
                "task_id": {**_STRING, "description": "Pending task to bind the worktree to."},
            },
            "required": ["name", "task_id"],
        },
        handler=_create_worktree,
        profiles=("lead",),
    ),
    ToolSpec(
        name="remove_worktree",
        description=(
            "Remove a completed task's Git worktree while retaining its branch. Refuses while "
            "the task is active, a teammate is using it, or uncommitted changes exist. Set "
            "discard_changes=true only after deciding those changes are expendable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "Worktree name."},
                "discard_changes": {
                    "type": "boolean",
                    "description": "Force removal and discard uncommitted files. Branch retained.",
                },
            },
            "required": ["name"],
        },
        handler=_remove_worktree,
        profiles=("lead",),
    ),
    ToolSpec(
        name="connect_mcp",
        description=(
            "Connect to an MCP server and discover its tools. Built-in mock servers are "
            "'docs' and 'deploy'; other servers come from .mcp.json. Discovered tools are "
            "named mcp__<server>__<tool> and become callable immediately. Servers that are "
            "not host-approved require interactive confirmation per call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "Server name, e.g. 'docs'."},
            },
            "required": ["name"],
        },
        handler=_connect_mcp,
        profiles=("lead",),
    ),
    ToolSpec(
        name="list_mcp_tools",
        description=(
            "List connected and available MCP servers with their tools and host policy "
            "(allow or confirm)."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_list_mcp_tools,
        profiles=("lead", "teammate"),
    ),
    ToolSpec(
        name="run_workflow",
        description=(
            "Run a host-registered workflow. The orchestration is trusted code, so you "
            "only choose the name and pass JSON args. Returns a JSON summary with runId, "
            "status, usage and result. If a run fails, resume it by passing that runId as "
            "resume_from_run_id with the original args; completed agent steps replay from "
            "the journal. Optional args.budget caps total workflow tokens."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {**_STRING, "description": "Workflow name from list_workflows."},
                "args": {
                    "type": "object",
                    "description": "Workflow-specific JSON input.",
                },
                "resume_from_run_id": {
                    **_STRING,
                    "description": "Optional runId of a failed run to resume.",
                },
            },
            "required": ["name"],
        },
        handler=_run_workflow,
        profiles=("lead",),
    ),
    ToolSpec(
        name="list_workflows",
        description="List registered workflows, their phases, and the last run.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_workflows,
        profiles=("lead", "teammate"),
    ),
]


def build_base_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in BASE_TOOLS:
        registry.register(spec)
    return registry
