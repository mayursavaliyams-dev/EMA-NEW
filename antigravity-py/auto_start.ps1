# Registers the 07:30 Mon-Fri auto start (called by AUTO_START_SETUP.bat).
# - no time limit      (Task Scheduler's default stops a task after 3 days)
# - start when available (PC was off at 07:30 -> starts as soon as it is on and you log in)
# - ignore a 2nd copy  (if the tracker is still running from yesterday, it keeps running)
param([Parameter(Mandatory = $true)][string]$Bat)
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory (Split-Path -Parent $Bat)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '07:30'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Stock Options Tracker 0730' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Stock Options + News tracker (display only, never places orders)' -Force | Out-Null
$t = Get-ScheduledTask -TaskName 'Stock Options Tracker 0730'
Write-Output ("  [OK] next run: " + ($t | Get-ScheduledTaskInfo).NextRunTime)
