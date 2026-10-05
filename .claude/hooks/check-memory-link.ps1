# SessionStart check: is this PC's Claude memory for the project linked to Google Drive?
# Silent when it is. If it isn't, prints a note that Claude reads at the start of the session.
$proj = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$key = $proj -replace '[^A-Za-z0-9]', '-'
$mem = Join-Path $env:USERPROFILE ".claude\projects\$key\memory"
$item = Get-Item $mem -Force -ErrorAction SilentlyContinue
if ($item -and $item.LinkType -eq 'Junction' -and (Test-Path $mem)) { exit 0 }

$state = if (-not $item) { "doesn't exist yet" } elseif ($item.LinkType -eq 'Junction') { "is linked but the Drive folder isn't reachable" } else { "is a plain local folder" }
@"
MEMORY NOT LINKED ON THIS PC: $mem $state, so notes saved on other PCs aren't visible here
and anything saved now won't reach them. Tell Derek at the start of your reply, in one or two plain
sentences. The fix: once Google Drive has finished syncing, run the "personal / non-Watson" junction
block in Step 1 of G:\My Drive\claude\HOME_BOOTSTRAP.md (regular PowerShell), then restart Claude.
Offer to run it for him. It keeps any existing local notes as memory.bak-<date>.
"@
