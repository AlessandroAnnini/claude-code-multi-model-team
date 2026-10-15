# Multi-model team reminders for Claude Code (Windows). No external services, no dependencies.
#
#   team_hook.ps1 session   -> SessionStart       (plain stdout is added to context)
#   team_hook.ps1 subagent  -> SubagentStart      (JSON additionalContext)
#   team_hook.ps1 triage    -> UserPromptSubmit   (keyword hints as JSON additionalContext)
#
# Any failure -> no output -> Claude Code's normal behaviour (fail open).
param([string]$Mode = "")
$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

switch ($Mode) {
  "session" {
    Write-Output "Multi-model team: you are the main session. Delegate repository discovery to the Explore and grep subagents and external documentation to the docs subagent; run independent searches in parallel and state the thoroughness level. Consult the advisor before locking a non-trivial plan, when the same failure occurs twice, and before declaring substantial work done."
  }

  "subagent" {
    Write-Output '{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"You are a read-only research subagent in a multi-model team. Return paths, symbols and concise findings only. Do not modify files, do not make architectural decisions, do not consult the advisor."}}'
  }

  "triage" {
    $input = [Console]::In.ReadToEnd()
    if (-not $input -or $input.Length -lt 80) { exit 0 }   # short prompts get no hints
    $lower = $input.ToLowerInvariant()
    $hints = @()
    if ($lower -match '\b(where|find|locate|which file|how (does|is|do)|implemented|call(s|ed|er|ers)?|references?|usages?|trace|defined)\b') {
      $hints += "Triage: the code involved may not be named in the request; repository discovery via Explore or grep fits here."
    }
    if ($lower -match '\b(library|framework|api|sdk|package|docs|documentation|version|deprecated|migration)\b') {
      $hints += "Triage: the request may depend on external library or API behavior; the docs agent fits here."
    }
    if ($hints.Count -eq 0) { exit 0 }
    $out = @{ hookSpecificOutput = @{ hookEventName = "UserPromptSubmit"; additionalContext = ($hints -join "`n") } }
    Write-Output ($out | ConvertTo-Json -Compress -Depth 3)
  }
}
exit 0
