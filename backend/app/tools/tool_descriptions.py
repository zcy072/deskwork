from app.sandbox.constants import COMMAND_TIMEOUT_SEC
from app.tools.executor import DEFAULT_VIEW_MAX_CHARS

WRITE_TODOS_DESCRIPTION = """Create or replace the agent's structured task list for multi-step file work.

## When to Use
- Multi-step file workflows (inspect → modify → deliver via `/output` + `present_files`)
- Multiple coordinated file operations
- User explicitly requests a plan or task breakdown

## When NOT to Use
- Greetings, casual chat, or simple Q&A
- Single-step actions (one file view, one edit, one answer)
- Trivial requests with an obvious single action

## Structure
- Each call replaces the entire todo list (not a partial update)
- Each todo has `content` (str) and `status` (`pending`, `in_progress`, or `completed`)
- Use clear, actionable step descriptions

## Best Practices
- Only one `in_progress` task at a time
- Mark `completed` as soon as a step is fully done
- Include a final step to save deliverables to `/output` and call `present_files` when sharing with the user
- Prune irrelevant items to keep the list focused

## Progress Updates
- Call `write_todos` again to change status or edit content
- Reflect real-time progress; do not batch completions
- If blocked, keep the task `in_progress` and add a new todo describing the blocker"""


READ_TODOS_DESCRIPTION = """Read the current todo list from agent state.

## When to Use
- After completing a step in a multi-step workflow to see what remains
- When re-orienting mid-task before deciding the next action

## When NOT to Use
- For simple requests that never needed a todo list
- When you already know the remaining steps without checking

## Returns
A formatted summary of all todos with status, or a message if the list is empty."""


_BASH_TOOL_TEMPLATE = """Execute a bash command in the sandboxed container filesystem.

Use for running Python scripts (especially for binary formats like PDF, DOCX, XLSX), shell utilities, and multi-step command pipelines.
Prefer direct file tools (`view_file`, `str_replace`, `create_file`, `insert`) for plain-text files.

Commands time out after {timeout} seconds by default; pass `timeout` (seconds, capped at {timeout}) for a shorter limit, and `restart=true` to reset a stuck shell. Do not run destructive or open-ended commands (e.g. `rm -rf /`, fork bombs, long-running servers)."""


BASH_TOOL_DESCRIPTION = _BASH_TOOL_TEMPLATE.format(timeout=COMMAND_TIMEOUT_SEC)


VIEW_FILE_DESCRIPTION = f"""View the contents of a plain-text file with line numbers.

Use only for text-based files (`.txt`, `.py`, `.md`, `.csv`, `.json`, etc.).
For binary formats (PDF, DOCX, XLSX, images), use `bash_tool` with an appropriate Python library instead.

Supports optional line range and character limit for large files. Output is capped at {DEFAULT_VIEW_MAX_CHARS} characters by default and ends with a truncation notice; use `view_range` to read the rest, or `search_document` to find specific passages."""


STR_REPLACE_DESCRIPTION = """Replace an exact string in a plain-text file.

The `old_string` must match exactly (including whitespace). Use only on text-based files.
For binary formats, use `bash_tool` with Python instead."""


CREATE_FILE_DESCRIPTION = """Create a new plain-text file with the given content.

Use `/workspace/scratchpad` for temporary or intermediate files.
Use `/workspace/output` for files you intend to share with the user (then call `present_files`).
Not for binary formats — use `bash_tool` with Python for those."""


INSERT_DESCRIPTION = """Insert text at a specific line in a plain-text file.

`insert_line=N` inserts the text after line N (1-based); `insert_line=0` inserts at the top of the file. Use only on text-based files.
For binary formats, use `bash_tool` with Python instead."""


PRESENT_FILES_DESCRIPTION = """Share one or more files with the user for download.

Files must already exist under `/workspace/output` before calling this tool.
Without this step, the user cannot see or download files you created.
Call after writing final deliverables to `/workspace/output`."""
