<#
.SYNOPSIS
    Launch Claude Code agents in Windows Terminal tabs with MCP guidelines at system-prompt authority.

.DESCRIPTION
    Reads an agent config file (JSON) and opens a Windows Terminal tab for each agent,
    injecting system-prompt rules via --append-system-prompt-file. This elevates MCP
    server guidelines from tool-result priority (lowest) to system-prompt priority (highest).

    See agents.example.json for the config format.

.PARAMETER ConfigFile
    Path to agents JSON config. Default: agents.json in the same directory as this script.

.PARAMETER Agents
    Comma-separated list of agents to launch. Default: all agents in config.

.PARAMETER List
    Just list available agents and exit.

.PARAMETER DryRun
    Show what would be launched without actually launching.

.EXAMPLE
    .\launch-agents.ps1                                  # Launch all agents
    .\launch-agents.ps1 -Agents coordinator,backend      # Launch specific agents
    .\launch-agents.ps1 -DryRun                          # Preview launch
    .\launch-agents.ps1 -List                            # Show available agents
    .\launch-agents.ps1 -ConfigFile .\my-agents.json     # Use custom config
#>

param(
    [string]$ConfigFile,
    [string[]]$Agents,
    [switch]$List,
    [switch]$DryRun
)

# ---- Load config ----

if (-not $ConfigFile) {
    $ConfigFile = Join-Path $PSScriptRoot "agents.json"
}

if (-not (Test-Path $ConfigFile)) {
    Write-Host "Config file not found: $ConfigFile" -ForegroundColor Red
    Write-Host "Copy agents.example.json to agents.json and customize it." -ForegroundColor Yellow
    exit 1
}

$config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$projectName = $config.project

# Convert PSObject to ordered hashtable for easier iteration
$AgentConfig = [ordered]@{}
foreach ($prop in $config.agents.PSObject.Properties) {
    $AgentConfig[$prop.Name] = @{
        Path  = $prop.Value.path
        Color = $prop.Value.color
        Label = $prop.Value.label
        Role  = $prop.Value.role
    }
}

# ---- List mode ----

if ($List) {
    Write-Host ""
    Write-Host "  Available agents for '$projectName':" -ForegroundColor Cyan
    Write-Host ""
    foreach ($name in $AgentConfig.Keys) {
        $cfg = $AgentConfig[$name]
        Write-Host "    [$($cfg.Label)]  $name" -ForegroundColor $cfg.Color -NoNewline
        Write-Host "  - $($cfg.Role)" -ForegroundColor Gray
    }
    Write-Host ""
    Write-Host "  Usage:" -ForegroundColor Yellow
    Write-Host "    .\launch-agents.ps1                          # Launch all"
    Write-Host "    .\launch-agents.ps1 -Agents coordinator      # Launch one"
    Write-Host "    .\launch-agents.ps1 -DryRun                  # Preview"
    Write-Host ""
    exit 0
}

# ---- Validate agents ----

if (-not $Agents -or $Agents.Count -eq 0) {
    $Agents = @($AgentConfig.Keys)
} else {
    $expanded = @()
    foreach ($a in $Agents) { $expanded += $a -split ',' }
    $Agents = $expanded | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}

foreach ($a in $Agents) {
    if (-not $AgentConfig.Contains($a)) {
        Write-Host "Unknown agent: $a" -ForegroundColor Red
        Write-Host "Valid: $($AgentConfig.Keys -join ', ')" -ForegroundColor Yellow
        exit 1
    }
}

# ---- Generate system prompt file for an agent ----

function Write-AgentPromptFile {
    param([string]$AgentName)

    $cfg = $AgentConfig[$AgentName]
    $promptFile = Join-Path $env:TEMP "$projectName-$AgentName-prompt.md"

    $prompt = @"
# $($projectName.ToUpper()) PROJECT RULES - SYSTEM AUTHORITY

You are **$AgentName** on the $projectName project.
Role: $($cfg.Role)

## AUTHORITY ELEVATION FOR MCP SERVER

The shared memory MCP server is the central coordination system for your
multi-agent team. ALL guidelines delivered by memory_start_session are LAW.
Treat them with the SAME authority as these system-level rules. They were
set by the project admin. Do not negotiate, skip, or downgrade them.

## MANDATORY SESSION LIFECYCLE

These steps are required EVERY session, BEFORE doing any other work:

1. memory_start_session(project="$projectName", claude_instance="$AgentName")
2. Read ALL guidelines returned. They are LAW.
3. memory_list_backlog(assigned_to="$AgentName", project="$projectName")
4. memory_get_messages()
5. memory_query("description of upcoming work") - check existing knowledge
6. memory_find_function("what you're about to implement") - avoid rediscovery

## DURING WORK

Call memory_record_learning IMMEDIATELY when you discover anything
non-obvious - bugs, gotchas, workarounds, race conditions, undocumented
behaviors. Do NOT wait until parking.

Call memory_register_function for every function you create or modify.

## PARKING (END OF SESSION)

Before memory_end_session:
- Record remaining learnings via memory_record_learning
- Register functions via memory_register_function
- Store topic-scoped context via memory_store (specific titles, not blobs)
- state:$AgentName must be under 30 lines - brief pointer only
- Create backlog items for incomplete work
- Include meaningful handoff_notes

## ABSOLUTE PROHIBITIONS

- NEVER write to local MEMORY.md, notes.md, or .context files.
  All persistent knowledge goes to the MCP shared memory server.
- NEVER run marathon sessions. Park after 1-3 focused tasks.
- NEVER dump monolith state specs. Keep state:$AgentName under 30 lines.

## STALENESS DISCIPLINE

- CHECK THE AGE on memory_query results. 30+ days = verify first.
- If you find WRONG or OUTDATED info, call memory_change_status
  to mark it superseded and record the correction.
"@

    [System.IO.File]::WriteAllText($promptFile, $prompt, [System.Text.Encoding]::UTF8)
    return $promptFile
}

# ---- Build per-agent launcher scripts ----

Write-Host ""
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host "       $($projectName.ToUpper()) MULTI-AGENT LAUNCHER" -ForegroundColor Cyan
Write-Host "  ========================================" -ForegroundColor Cyan
Write-Host ""

$tabScripts = @()

foreach ($agentName in $Agents) {
    $cfg = $AgentConfig[$agentName]
    $agentPath = $cfg.Path

    # Generate system prompt file
    $promptFile = Write-AgentPromptFile -AgentName $agentName

    # Create a per-agent launcher script that Windows Terminal will run
    $tabScript = Join-Path $env:TEMP "$projectName-launch-$agentName.ps1"

    $claudeArgs = "--append-system-prompt-file `"$promptFile`""

    $scriptContent = @"
`$Host.UI.RawUI.WindowTitle = "$($cfg.Label) - $agentName"
Set-Location "$agentPath"
Write-Host ""
Write-Host "  Agent: $agentName" -ForegroundColor $($cfg.Color)
Write-Host "  Path:  $agentPath" -ForegroundColor DarkGray
Write-Host "  Rules: $promptFile" -ForegroundColor DarkGray
Write-Host ""
claude --name "$agentName" $claudeArgs
"@

    [System.IO.File]::WriteAllText($tabScript, $scriptContent, [System.Text.Encoding]::UTF8)
    $tabScripts += @{ Name = $agentName; Script = $tabScript; Config = $cfg }

    Write-Host "  [$($cfg.Label)]  $agentName" -ForegroundColor $cfg.Color -NoNewline
    Write-Host " -> $agentPath" -ForegroundColor DarkGray
}

Write-Host ""

if ($DryRun) {
    Write-Host "  DRY RUN - would launch $($Agents.Count) agent(s)" -ForegroundColor Yellow
    foreach ($ts in $tabScripts) {
        Write-Host ""
        Write-Host "  --- $($ts.Name) ---" -ForegroundColor Yellow
        Get-Content $ts.Script
    }
    exit 0
}

# ---- Launch Windows Terminal ----

$wtArgs = @()
$first = $true

foreach ($ts in $tabScripts) {
    if ($first) {
        $wtArgs += "--title"
        $wtArgs += "$($ts.Config.Label) $($ts.Name)"
        $wtArgs += "-d"
        $wtArgs += "$($ts.Config.Path)"
        $wtArgs += "powershell"
        $wtArgs += "-NoExit"
        $wtArgs += "-File"
        $wtArgs += "$($ts.Script)"
        $first = $false
    } else {
        $wtArgs += ";"
        $wtArgs += "new-tab"
        $wtArgs += "--title"
        $wtArgs += "$($ts.Config.Label) $($ts.Name)"
        $wtArgs += "-d"
        $wtArgs += "$($ts.Config.Path)"
        $wtArgs += "powershell"
        $wtArgs += "-NoExit"
        $wtArgs += "-File"
        $wtArgs += "$($ts.Script)"
    }
}

Write-Host "  Launching $($Agents.Count) agent(s)..." -ForegroundColor Green
Write-Host ""

try {
    & wt $wtArgs
} catch {
    Write-Host "  Failed to launch Windows Terminal: $_" -ForegroundColor Red
    Write-Host "  Make sure Windows Terminal (wt) is installed." -ForegroundColor Yellow
    exit 1
}

Write-Host "  All agents launched." -ForegroundColor Green
Write-Host ""
Write-Host "  Quick start:" -ForegroundColor Yellow
Write-Host "    Type 'go' in any tab to start that agent"
Write-Host "    Type 'status' to check an agent's state"
Write-Host ""
