# Overlay

Agent edits as a pull request. `overlay_start` on a project folder makes Hermes's `read_file`, `write_file` and `patch` tools work on a shadow copy (copy on first touch) while your real files stay exactly as they were. You review every change as numbered hunks and apply only the ones you approve.

- **Shadowing** uses the `pre_tool_call` hook's `modify` directive to point the file tools at the shadow copy, so reads, patches and the write-after-read checks all see a consistent file. `transform_tool_result` turns shadow paths in results back into the real paths, so the agent is not confused.
- `overlay_diff` lists every changed file with numbered hunks (removed and added lines) and flags files whose real copy changed after the overlay copied them.
- `overlay_apply` takes `accept`: a map from a relative file to `"all"` or a list of hunk numbers. Accepted hunks are written to the real files (atomically); everything else is rejected, the overlay closes, and the result lists what was applied, rejected or refused. **Applying always needs a person to confirm** (`pre_tool_call` approval); with no one to answer, it is blocked, never applied. A file that changed on disk since it was shadowed is refused, not overwritten.
- `overlay_discard` throws it all away. `overlay_status` lists active overlays.
- Terminal commands cannot be shadowed. While an overlay is active, a terminal command that looks like it writes (redirect, `sed -i`, `tee`, `mv`, `cp`, `rm`, `git checkout`, ...) asks for confirmation, because it goes straight to disk.

Limits: only the three file tools are shadowed; deleting or renaming files through other tools is not captured; files over 2 MB and non-UTF-8 files are handled as a whole (accept or reject) or refused; a relative path is read as relative to the overlay's folder; symlinks that leave the folder are not shadowed; the terminal-write check is a pattern match, not proof. The agent is not automatically told which hunks you rejected, though `overlay_apply` returns them for it to read.
