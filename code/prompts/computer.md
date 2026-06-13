The Computer-Use skill drives the real desktop. It walks a small cascade
starting from the cheapest deterministic path (launch an app and run any
caller-supplied coordinate/keystroke steps) and escalating to a vision
loop when the goal needs perception. The escalation is internal; you pass
`goal` and the skill chooses the layer.

The driver is `cua.Localhost.connect()` — direct, unsandboxed host control.
There is no accessibility tree, so the skill addresses the screen by pixel
coordinate: each vision turn screenshots the desktop, asks the gateway's
vision model for the next action, and clicks/types via cua's mouse and
keyboard. Every model call routes through the V9 gateway tagged
`agent="computer"`.

Inputs: `metadata.goal` (required, free-text description of what to do),
`metadata.app` (optional, an app to launch first, e.g. "calc"),
`metadata.actions` (optional, a list of deterministic steps to run instead
of the vision loop), `metadata.max_steps` (optional turn cap). Output:
`ComputerOutput` with `actions` (the per-turn trace), `path` reporting the
cascade layer that ran (deterministic / vision), and `final_title` (the
active-window title at the end of the run). Use this skill when the task is
on the desktop rather than the web — driving native apps, system dialogs,
or anything Browser's URL-based cascade cannot reach.
