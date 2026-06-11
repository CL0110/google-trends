# Pulls the latest notes committed by the GitHub Action and copies them into
# the Obsidian vault (OneDrive). Run by the "GoogleTrendsScraper" scheduled task.
# Kept out of OneDrive so .git never syncs and corrupts.

$ErrorActionPreference = "Stop"
$git   = "C:\Program Files\Git\cmd\git.exe"
$repo  = "C:\Users\clair\GoogleTrends"
$vault = "C:\Users\clair\OneDrive\Documents\Learning and Skills\Obsidian Vault\Google Trends"

# 1. Pull whatever the cloud job committed (fast-forward only — repo is read-only locally)
& $git -C $repo pull --ff-only

# 2. Copy the note files into the vault (plain files; OneDrive then syncs them)
New-Item -ItemType Directory -Force -Path $vault | Out-Null
Copy-Item "$repo\notes\trends_*.md"   $vault -Force
Copy-Item "$repo\notes\trends_*.json" $vault -Force

Write-Host "Synced notes from GitHub -> $vault"
