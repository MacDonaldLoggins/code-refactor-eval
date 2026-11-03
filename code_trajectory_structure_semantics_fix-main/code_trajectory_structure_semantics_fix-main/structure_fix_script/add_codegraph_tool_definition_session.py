#!/usr/bin/env python3
"""Add missing tool definitions (codegraph, playwright, ...) to matching session traces.

Registry-driven: to cover a new tool, append its definition to TOOL_DEFINITIONS.
For each session file, every registered tool is handled independently — a file
missing multiple definitions gets all of them appended in one pass.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_FAILED_DIR = PROJECT_ROOT / "data_failed"

SCRIPT_NAME = "add_codegraph_tool_definition_session"
FUNCTION_NAME = "add_codegraph_tool_definition"
REPORT_PROBLEM = "top-level tools[] missing MCP tool definitions referenced in messages"


# Registry of tool definitions to backfill.
# Key: tool name (must equal the "name" field). Value: the full tool definition
# object as it should appear inside the session JSON's top-level "tools" list.
TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "mcp__codegraph__codegraph_explore": {
        "name": "mcp__codegraph__codegraph_explore",
        "description": (
            "PRIMARY TOOL — call FIRST for almost any question OR before an edit: "
            "how does X work, architecture, a bug, where/what is X, surveying an "
            "area, or the symbols you are about to change. Returns the verbatim "
            "source of the relevant symbols grouped by file in ONE capped call "
            "(Read-equivalent — treat the shown source as already Read; do NOT "
            "re-open those files), plus the call path among them. Query can be a "
            "natural-language question OR a bag of symbol/file names. Usually the "
            "ONLY call you need — more accurate context, in far fewer tokens and "
            "round-trips than a search/Read/Grep loop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Symbol names, file names, or short code terms to explore "
                        '(e.g., "AuthService loginUser session-manager", '
                        '"GraphTraverser BFS impact traversal.ts"). For a flow '
                        "question, name the symbols spanning the flow (e.g. "
                        '"mutateElement renderScene"). A natural-language question '
                        "works too — no prior codegraph_search needed."
                    ),
                },
                "maxFiles": {
                    "type": "number",
                    "description": "Maximum number of files to include source code from (default: 12)",
                    "default": 12,
                },
                "projectPath": {
                    "type": "string",
                    "description": (
                        "Absolute path to the project to query (or any directory "
                        "inside it) — codegraph uses the nearest .codegraph/ index "
                        "at or above that path. Omit to use this session's default "
                        "project. Pass it to query a second codebase, or when the "
                        "server root has no index of its own (e.g. a monorepo where "
                        "only sub-projects are indexed, so there is no default project)."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    # Playwright tools are registered below from the raw JSON constant.
}


# Raw JSON payload copied verbatim from Claude Code's playwright MCP server
# `tools/list` response. The bare "browser_*" names get an "mcp__playwright__"
# prefix at load time to match how they appear in session traces. Kept as a raw
# triple-quoted string so JSON's `\"` escapes survive Python parsing unchanged.
_PLAYWRIGHT_TOOLS_JSON = r"""
{
  "tools": [
    {
      "name": "browser_close",
      "description": "Close the page",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {
        "title": "Close browser",
        "readOnlyHint": false,
        "destructiveHint": true,
        "openWorldHint": true
      }
    },
    {
      "name": "browser_resize",
      "description": "Resize the browser window",
      "inputSchema": {
        "type": "object",
        "properties": {
          "width": {"type": "number", "description": "Width of the browser window"},
          "height": {"type": "number", "description": "Height of the browser window"}
        },
        "required": ["width", "height"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Resize browser window", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_console_messages",
      "description": "Returns all console messages",
      "inputSchema": {
        "type": "object",
        "properties": {
          "level": {
            "default": "info",
            "description": "Level of the console messages to return. Each level includes the messages of more severe levels. Defaults to \"info\".",
            "type": "string",
            "enum": ["error", "warning", "info", "debug"]
          },
          "all": {
            "description": "Return all console messages since the beginning of the session, not just since the last navigation. Defaults to false.",
            "type": "boolean"
          },
          "filename": {
            "description": "Filename to save the console messages to. If not provided, messages are returned as text.",
            "type": "string"
          }
        },
        "required": ["level"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Get console messages", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_handle_dialog",
      "description": "Handle a dialog",
      "inputSchema": {
        "type": "object",
        "properties": {
          "accept": {"type": "boolean", "description": "Whether to accept the dialog."},
          "promptText": {"description": "The text of the prompt in case of a prompt dialog.", "type": "string"}
        },
        "required": ["accept"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Handle a dialog", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_evaluate",
      "description": "Evaluate JavaScript expression on page or element",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"description": "Exact target element reference from the page snapshot, or a unique element selector", "type": "string"},
          "function": {"type": "string", "description": "() => { /* code */ } or (element) => { /* code */ } when element is provided"},
          "filename": {"description": "Filename to save the result to. If not provided, result is returned as text.", "type": "string"}
        },
        "required": ["function"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Evaluate JavaScript", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_file_upload",
      "description": "Upload one or multiple files",
      "inputSchema": {
        "type": "object",
        "properties": {
          "paths": {
            "description": "The absolute paths to the files to upload. Can be single file or multiple files. If omitted, file chooser is cancelled.",
            "type": "array",
            "items": {"type": "string"}
          }
        },
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Upload files", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_drop",
      "description": "Drop files or MIME-typed data onto an element, as if dragged from outside the page. At least one of \"paths\" or \"data\" must be provided.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
          "paths": {"description": "Absolute paths to files to drop onto the element.", "type": "array", "items": {"type": "string"}},
          "data": {
            "description": "Data to drop, as a map of MIME type to string value (e.g. {\"text/plain\": \"hello\", \"text/uri-list\": \"https://example.com\"}).",
            "type": "object",
            "propertyNames": {"type": "string"},
            "additionalProperties": {"type": "string"}
          }
        },
        "required": ["target"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Drop files or data onto an element", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_find",
      "description": "Search the accessibility snapshot of the current page for text or a regular expression. Returns matching snapshot nodes with a few lines of surrounding context (like search snippets), each shown under its path from the root of the tree, which is cheaper than capturing the whole snapshot when you only need to locate an element and its ref.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "text": {
            "description": "Plain text to search for in the page snapshot (case-insensitive substring match). Provide either text or regex, not both.",
            "type": "string"
          },
          "regex": {
            "description": "Regular expression to search for in the page snapshot. Matching is case-sensitive by default; wrap the pattern in slashes to add flags, e.g. \"/error/i\" for case-insensitive. Provide either text or regex, not both.",
            "type": "string"
          }
        },
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Find in page snapshot", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_fill_form",
      "description": "Fill multiple form fields",
      "inputSchema": {
        "type": "object",
        "properties": {
          "fields": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
                "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
                "name": {"type": "string", "description": "Human-readable field name"},
                "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"], "description": "Type of the field"},
                "value": {"type": "string", "description": "Value to fill in the field. If the field is a checkbox, the value should be `true` or `false`. If the field is a combobox, the value should be the text of the option."}
              },
              "required": ["target", "name", "type", "value"],
              "additionalProperties": false
            },
            "description": "Fields to fill in"
          }
        },
        "required": ["fields"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Fill form", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_press_key",
      "description": "Press a key on the keyboard",
      "inputSchema": {
        "type": "object",
        "properties": {
          "key": {"type": "string", "description": "Name of the key to press or a character to generate, such as `ArrowLeft` or `a`"}
        },
        "required": ["key"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Press a key", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_type",
      "description": "Type text into editable element",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
          "text": {"type": "string", "description": "Text to type into the element"},
          "submit": {"description": "Whether to submit entered text (press Enter after)", "type": "boolean"},
          "slowly": {"description": "Whether to type one character at a time. Useful for triggering key handlers in the page. By default entire text is filled in at once.", "type": "boolean"}
        },
        "required": ["target", "text"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Type text", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_navigate",
      "description": "Navigate to a URL",
      "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
        "required": ["url"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Navigate to a URL", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_navigate_back",
      "description": "Go back to the previous page in the history",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Go back", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_network_requests",
      "description": "Returns a numbered list of network requests since loading the page. Use browser_network_request with the number to get full details.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "static": {"default": false, "description": "Whether to include successful static resources like images, fonts, scripts, etc. Defaults to false.", "type": "boolean"},
          "filter": {"description": "Only return requests whose URL matches this regexp (e.g. \"/api/.*user\").", "type": "string"},
          "filename": {"description": "Filename to save the network requests to. If not provided, requests are returned as text.", "type": "string"}
        },
        "required": ["static"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "List network requests", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_network_request",
      "description": "Returns full details (headers and body) of a single network request, or a single part if `part` is set. Use the number from browser_network_requests.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "index": {"type": "integer", "minimum": 1, "maximum": 9007199254740991, "description": "1-based index of the request, as printed by browser_network_requests."},
          "part": {"description": "Return only this part of the request. Omit to return full details.", "type": "string", "enum": ["request-headers", "request-body", "response-headers", "response-body"]},
          "filename": {"description": "Filename to save the result to. If not provided, output is returned as text.", "type": "string"}
        },
        "required": ["index"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Show network request details", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_run_code_unsafe",
      "description": "Run a Playwright code snippet. Unsafe: executes arbitrary JavaScript in the Playwright server process and is RCE-equivalent.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "code": {"description": "A JavaScript function containing Playwright code to execute. It will be invoked with a single argument, page, which you can use for any page interaction. For example: `async (page) => { await page.getByRole('button', { name: 'Submit' }).click(); return await page.title(); }`", "type": "string"},
          "filename": {"description": "Load code from the specified file. If both code and filename are provided, code will be ignored.", "type": "string"}
        },
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Run Playwright code (unsafe)", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_take_screenshot",
      "description": "Take a screenshot of the current page. You can't perform actions based on the screenshot, use browser_snapshot for actions.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"description": "Exact target element reference from the page snapshot, or a unique element selector", "type": "string"},
          "type": {"default": "png", "description": "Image format for the screenshot. Default is png.", "type": "string", "enum": ["png", "jpeg"]},
          "filename": {"description": "File name to save the screenshot to. Defaults to `page-{timestamp}.{png|jpeg}` if not specified. Prefer relative file names to stay within the output directory.", "type": "string"},
          "fullPage": {"description": "When true, takes a screenshot of the full scrollable page, instead of the currently visible viewport. Cannot be used with element screenshots.", "type": "boolean"},
          "scale": {"default": "css", "description": "Image resolution scale. \"css\" produces a screenshot sized in CSS pixels (smaller, consistent across devices). \"device\" produces a high-resolution screenshot using device pixels (larger, accounts for the device pixel ratio). Default is css.", "type": "string", "enum": ["css", "device"]}
        },
        "required": ["type", "scale"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Take a screenshot", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_snapshot",
      "description": "Capture accessibility snapshot of the current page, this is better than screenshot",
      "inputSchema": {
        "type": "object",
        "properties": {
          "target": {"description": "Exact target element reference from the page snapshot, or a unique element selector", "type": "string"},
          "filename": {"description": "Save snapshot to markdown file instead of returning it in the response.", "type": "string"},
          "depth": {"description": "Limit the depth of the snapshot tree", "type": "number"},
          "boxes": {"description": "Include each element's bounding box as [box=x,y,width,height] in the snapshot. Coordinates are viewport-relative, in CSS pixels (Element.getBoundingClientRect)", "type": "boolean"}
        },
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Page snapshot", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    },
    {
      "name": "browser_click",
      "description": "Perform click on a web page",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
          "doubleClick": {"description": "Whether to perform a double click instead of a single click", "type": "boolean"},
          "button": {"description": "Button to click, defaults to left", "type": "string", "enum": ["left", "right", "middle"]},
          "modifiers": {"description": "Modifier keys to press", "type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]}}
        },
        "required": ["target"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Click", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_drag",
      "description": "Perform drag and drop between two elements",
      "inputSchema": {
        "type": "object",
        "properties": {
          "startElement": {"description": "Human-readable source element description used to obtain the permission to interact with the element", "type": "string"},
          "startTarget": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
          "endElement": {"description": "Human-readable target element description used to obtain the permission to interact with the element", "type": "string"},
          "endTarget": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"}
        },
        "required": ["startTarget", "endTarget"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Drag mouse", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_hover",
      "description": "Hover over element on page",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"}
        },
        "required": ["target"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Hover mouse", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_select_option",
      "description": "Select an option in a dropdown",
      "inputSchema": {
        "type": "object",
        "properties": {
          "element": {"description": "Human-readable element description used to obtain permission to interact with the element", "type": "string"},
          "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
          "values": {"type": "array", "items": {"type": "string"}, "description": "Array of values to select in the dropdown. This can be a single value or multiple values."}
        },
        "required": ["target", "values"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Select option", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_tabs",
      "description": "List, create, close, or select a browser tab.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "action": {"type": "string", "enum": ["list", "new", "close", "select"], "description": "Operation to perform"},
          "index": {"description": "Tab index, used for close/select. If omitted for close, current tab is closed.", "type": "number"},
          "url": {"description": "URL to navigate to in the new tab, used for new.", "type": "string"}
        },
        "required": ["action"],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Manage tabs", "readOnlyHint": false, "destructiveHint": true, "openWorldHint": true}
    },
    {
      "name": "browser_wait_for",
      "description": "Wait for text to appear or disappear or a specified time to pass",
      "inputSchema": {
        "type": "object",
        "properties": {
          "time": {"description": "The time to wait in seconds", "type": "number"},
          "text": {"description": "The text to wait for", "type": "string"},
          "textGone": {"description": "The text to wait for to disappear", "type": "string"}
        },
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": false
      },
      "annotations": {"title": "Wait for", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": true}
    }
  ]
}
"""


def _register_prefixed_tools(payload_json: str, prefix: str, target: dict[str, dict[str, Any]]) -> None:
    """Load tools from raw MCP tools/list JSON and register each with a name prefix.

    The bare "name" from the payload is rewritten to f"{prefix}{name}" (the form
    session traces use for MCP tools) before being stored.
    """
    payload = json.loads(payload_json)
    for tool in payload.get("tools", []):
        raw_name = tool.get("name")
        if not isinstance(raw_name, str):
            continue
        prefixed = f"{prefix}{raw_name}"
        tool["name"] = prefixed
        target[prefixed] = tool


_register_prefixed_tools(_PLAYWRIGHT_TOOLS_JSON, "mcp__playwright__", TOOL_DEFINITIONS)


def _extract_session_id(data: Any, path: Path) -> str:
    """Prefer sessionId inside the JSON, fall back to the file stem."""
    if isinstance(data, dict):
        for key in ("sessionId", "session_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return path.stem


def _extract_session_timestamp(data: Any) -> str | None:
    """Return the first message's timestamp field, if present."""
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if isinstance(first, dict):
        ts = first.get("timestamp")
        if isinstance(ts, str):
            return ts
    return None


def _tool_names_in_order(tools: list[Any]) -> list[str]:
    """Ordered list of tool names from a tools[] list (skip malformed entries)."""
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _task_dir_of_session_file(path: Path) -> Path:
    """Locate the task directory that owns this session file.

    Supports flat (<task>/<task>.json) and legacy (<task>/sessions/**/*.json)
    layouts; falls back to the file's parent when neither pattern matches so
    a mis-shaped tree still yields a report location.
    """
    resolved = path.resolve()
    parent = resolved.parent
    if resolved.name == f"{parent.name}.json":
        return parent
    for anc in resolved.parents:
        if anc.name == "sessions":
            return anc.parent
    return parent


def iter_session_files(tasks_dir: Path, task_glob: str = "TASK-*") -> list[Path]:
    files: list[Path] = []
    task_dirs = (
        [tasks_dir]
        if tasks_dir.is_dir() and tasks_dir.match(task_glob)
        else sorted(tasks_dir.glob(task_glob))
    )
    for task_dir in task_dirs:
        if not task_dir.is_dir():
            continue
        # Flat layout: <task>/<task>.json
        flat = task_dir / f"{task_dir.name}.json"
        if flat.is_file():
            files.append(flat)
            continue
        # Legacy layout: <task>/sessions/**/*.json
        sessions_dir = task_dir / "sessions"
        if sessions_dir.is_dir():
            files.extend(path for path in sessions_dir.rglob("*.json") if path.is_file())
    return sorted(files)


def existing_tool_names(tools: list[Any]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def add_definitions_to_top_level_tools(
    data: Any, needed: list[str]
) -> tuple[int, str | None]:
    if not isinstance(data, dict):
        return 0, "json root is not an object"

    tools = data.setdefault("tools", [])
    if not isinstance(tools, list):
        return 0, 'top-level "tools" is not a list'

    present = existing_tool_names(tools)
    added = 0
    for name in needed:
        if name in present:
            continue
        tools.append(TOOL_DEFINITIONS[name])
        added += 1
    return added, None


def process_file(path: Path, *, dry_run: bool) -> tuple[dict[str, Any] | None, str | None]:
    """Process a single session file.

    Returns (record, error). On error, record is None and error carries the
    message. Otherwise record contains every field the JSON report needs
    (session id/timestamp, tool names before and after, and referenced /
    added / already-present name lists). Callers read record['matched'] and
    record['added'] to drive counters and to decide whether to emit a report
    sub-entry.
    """
    try:
        original = path.read_text(encoding="utf-8-sig")
        needed = [name for name in TOOL_DEFINITIONS if name in original]
        if not needed:
            return {
                "matched": False,
                "added": 0,
                "session_id": path.stem,
                "session_timestamp": None,
                "tools_before": [],
                "tools_after": [],
                "referenced": [],
                "added_names": [],
                "already_present": [],
                "added_definitions": {},
            }, None

        data = json.loads(original)
        if not isinstance(data, dict):
            return None, "json root is not an object"

        session_id = _extract_session_id(data, path)
        session_timestamp = _extract_session_timestamp(data)

        raw_tools = data.get("tools")
        if raw_tools is not None and not isinstance(raw_tools, list):
            return None, 'top-level "tools" is not a list'
        tools_before = _tool_names_in_order(
            raw_tools if isinstance(raw_tools, list) else []
        )

        added, error = add_definitions_to_top_level_tools(data, needed)
        if error:
            return None, error

        tools_after = _tool_names_in_order(data.get("tools", []))
        before_set = set(tools_before)
        added_names = [name for name in needed if name not in before_set]
        already_present = [name for name in needed if name in before_set]
        added_definitions = {
            name: TOOL_DEFINITIONS[name] for name in added_names
        }

        if added > 0 and not dry_run:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        return {
            "matched": True,
            "added": added,
            "session_id": session_id,
            "session_timestamp": session_timestamp,
            "tools_before": tools_before,
            "tools_after": tools_after,
            "referenced": needed,
            "added_names": added_names,
            "already_present": already_present,
            "added_definitions": added_definitions,
        }, None
    except UnicodeError:
        return None, "not utf-8"
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"
    except OSError as exc:
        return None, f"io error: {exc}"
    except Exception as exc:
        return None, f"unexpected error: {exc}"


def session_context_for_file(path: Path) -> tuple[Path, Path]:
    resolved = path.resolve()
    task_dir = resolved.parent
    # Flat layout: <task>/<task>.json
    if resolved.name == f"{task_dir.name}.json":
        return task_dir, Path(resolved.name)
    for parent in resolved.parents:
        if parent.name == "sessions":
            return parent.parent, resolved.relative_to(parent)
    raise ValueError(f"not under a sessions/ directory: {path}")


def latest_session_version_dir(task_dir: Path) -> Path | None:
    latest_num = 0
    latest_dir: Path | None = None
    for path in task_dir.glob("sessions_v*"):
        if not path.is_dir():
            continue
        suffix = path.name.removeprefix("sessions_v")
        if not suffix.isdigit():
            continue
        version_num = int(suffix)
        if version_num > latest_num:
            latest_num = version_num
            latest_dir = path
    return latest_dir


def _delete_session_file(path: Path, *, dry_run: bool) -> bool:
    if not (path.is_file() or path.is_symlink()):
        return False
    if not dry_run:
        path.unlink()
    return True


def move_error_session_to_failed(
    session_file: Path,
    failed_dir: Path,
    *,
    dry_run: bool,
) -> tuple[int, list[str], str | None]:
    try:
        task_dir, session_rel = session_context_for_file(session_file)
        is_flat = session_rel.name == f"{task_dir.name}.json"
        messages: list[str] = []

        if is_flat:
            source = task_dir / f"{task_dir.name}_original.json"
            dest = failed_dir / f"{task_dir.name}.json"
            if not source.is_file():
                return 0, [], f"missing source session: {source}"
            if dry_run:
                messages.append(f"[error-session:dry-run] {source} -> {dest}")
            else:
                failed_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                shutil.copy2(str(source), str(dest))
                messages.append(f"[error-session:copy] {task_dir.name}/{source.name} -> {dest}")

            removed = 0
            for path in [
                task_dir / f"{task_dir.name}.json",
                *task_dir.glob(f"{task_dir.name}_outer_*_inner_*.json"),
            ]:
                if _delete_session_file(path, dry_run=dry_run):
                    removed += 1
                    action = "would remove" if dry_run else "remove"
                    messages.append(f"[error-session:{action}] {task_dir.name}/{path.name}")
            return 1, messages, None

        # Legacy layout
        source = task_dir / "source_sessions" / session_rel
        dest = failed_dir / task_dir.name / "sessions" / session_rel
        if not source.is_file():
            return 0, [], f"missing source session: {source}"

        if dry_run:
            messages.append(f"[error-session:dry-run] {source} -> {dest}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            shutil.move(str(source), str(dest))
            messages.append(f"[error-session:move] {task_dir.name}/source_sessions/{session_rel.as_posix()} -> {dest}")

        removed = 0
        roots = [task_dir / "sessions"]
        latest = latest_session_version_dir(task_dir)
        if latest is not None:
            roots.append(latest)
        for root in roots:
            path = root / session_rel
            if _delete_session_file(path, dry_run=dry_run):
                removed += 1
                action = "would remove" if dry_run else "remove"
                messages.append(f"[error-session:{action}] {task_dir.name}/{root.name}/{session_rel.as_posix()}")
        return 1, messages, None
    except Exception as exc:
        return 0, [], f"failed to export error session {session_file}: {exc}"


def _build_report_sub_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one process_file record into report `sessions[].sub[]` entries.

    Convention aligned with sibling scripts (remove_task_tools / dedup /
    clean_empty): one `sub` entry per atomic change. Here each newly added
    tool definition becomes its own sub entry, so `before_content` /
    `after_content` describe exactly what was written to disk:
      - `before_content`: null — the tool was absent from tools[] (that is the
        precondition for adding it; already-present tools are skipped and
        never produce a sub entry).
      - `after_content`: the full tool-definition dict just appended, stored
        as a structured object (not a JSON-in-string) so it is human-readable
        in the report without extra un-escaping.
      - `line`: `tools[<index>]` — the 0-based position of the added
        definition in the file's post-modification tools[] array.
    """
    tools_after = record["tools_after"]
    after_index = {name: idx for idx, name in enumerate(tools_after)}
    added_defs: dict[str, dict[str, Any]] = record["added_definitions"]

    entries: list[dict[str, Any]] = []
    for name in record["added_names"]:
        definition = added_defs.get(name)
        idx = after_index.get(name)
        line_str = f"tools[{idx}]" if idx is not None else "tools[]"
        entries.append(
            {
                "function": FUNCTION_NAME,
                "action_type": "add",
                "before_content": None,
                "after_content": definition,
                "problem": REPORT_PROBLEM,
                "line": line_str,
                "addition": {
                    "tool_name": name,
                    "position": idx,
                    "tools_before_count": len(record["tools_before"]),
                    "tools_after_count": len(tools_after),
                    "session_timestamp": record["session_timestamp"],
                    "outcome": "success",
                },
            }
        )
    return entries


def write_task_report(
    task_dir: Path,
    records: list[dict[str, Any]],
    total_trajectory_count: int,
) -> Path:
    """Write a per-task JSON report under <task_dir>/report/.

    Only records with added > 0 should be passed in — this function does not
    filter. `total_trajectory_count` is the count of ALL session files scanned
    in the task (matched or not), so the summary reflects hit-rate not just
    hits. total_issues == sum of definitions_added across records.
    """
    report_dir = task_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{SCRIPT_NAME}_report.json"

    sessions = [
        {"session_id": r["session_id"], "sub": _build_report_sub_entries(r)}
        for r in records
    ]

    task_report = {
        "report_type": f"{SCRIPT_NAME}_report",
        "task_id": task_dir.name,
        "task_path": str(task_dir.resolve()),
        "create": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total_trajectory_count": total_trajectory_count,
            "hit_trajectory_count": len(records),
            "total_issues": sum(r["added"] for r in records),
        },
        "sessions": sessions,
    }

    report_file.write_text(
        json.dumps(task_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan TASK-*/sessions/**/*.json under a tasks directory. For each "
            "tool registered in TOOL_DEFINITIONS, if a session file references "
            'the tool by name, ensure the corresponding definition is present '
            'in the top-level "tools" list.'
        )
    )
    parser.add_argument("tasks_dir", type=Path, help="含 task 目录的根目录，例如 target-issues-tasks")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要修改的文件，不写入")
    parser.add_argument("--failed-dir", type=Path, default=DEFAULT_FAILED_DIR,
                        help="directory for source_sessions JSON files that cannot be processed")
    parser.add_argument("--task-glob", default="TASK-*",
                        help="glob pattern for task directories (default: TASK-*)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tasks_dir = args.tasks_dir.resolve()
    if not tasks_dir.is_dir():
        print(f"error: tasks_dir does not exist or is not a directory: {tasks_dir}")
        return 2

    # Group files by the task that owns them so each per-task report can carry
    # an accurate total_trajectory_count (all sessions, not just hits).
    files_by_task: dict[Path, list[Path]] = {}
    for path in iter_session_files(tasks_dir, task_glob=args.task_glob):
        task_dir = _task_dir_of_session_file(path)
        files_by_task.setdefault(task_dir, []).append(path)

    files_seen = files_matched = files_changed = definitions_added = errors = 0
    reports_written = 0
    for task_dir, paths in files_by_task.items():
        task_records: list[dict[str, Any]] = []
        for path in paths:
            files_seen += 1
            record, error = process_file(path, dry_run=args.dry_run)
            if error:
                errors += 1
                # Log-only: do NOT move/delete on a parse/structure error. A broken working
                # <sid>.json is almost always codex having mangled it during inner-loop
                # iteration; the immutable <sid>_original.json is intact and the outer-round
                # reset restores it. If it never recovers, the session is surfaced in
                # --unfixed-dir by the per-round snapshot (which always keeps the good
                # original as a restart point). Exporting here would copy the *good* original
                # under a "failed" label and destroy the real evidence.
                print(f"[error] {path}: {error} (left in place; outer-round reset restores original)")
                continue
            # record is guaranteed non-None when error is None
            if record["matched"]:
                files_matched += 1
            if record["added"]:
                files_changed += 1
                definitions_added += record["added"]
                action = "would update" if args.dry_run else "updated"
                print(f"[{action}] {path} tools_added={record['added']}")
                task_records.append(record)

        if task_records:
            report_file = write_task_report(task_dir, task_records, len(paths))
            reports_written += 1
            print(f"[task-report] {report_file}")

    mode = "dry_run" if args.dry_run else "write"
    print(
        f"[done] mode={mode} files_seen={files_seen} files_matched={files_matched} "
        f"files_changed={files_changed} definitions_added={definitions_added} "
        f"reports_written={reports_written} errors={errors}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
