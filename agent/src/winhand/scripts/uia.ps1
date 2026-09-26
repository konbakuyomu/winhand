# UI Automation helper for winhand's `ui` tool (Windows PowerShell 5.1 or 7).
# inspect: list the controls of a window; act: find one control and invoke/toggle/select/
# expand/collapse/set a value/focus it. Output is one JSON document on stdout.
param(
    [Parameter(Mandatory)][long] $Hwnd,
    [ValidateSet('inspect', 'act')][string] $Mode = 'inspect',
    [string] $Name = '',
    [string] $AutomationId = '',
    [string] $ControlType = '',
    [int] $Index = 0,
    [string] $Do = 'invoke',
    [string] $Value = '',
    [int] $MaxElements = 250,
    [string] $Filter = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [Windows.Automation.AutomationElement]
$root = $A::FromHandle([IntPtr]$Hwnd)
if (-not $root) { throw "no window with handle $Hwnd" }

function Patterns($e) {
    $names = @()
    foreach ($p in $e.GetSupportedPatterns()) { $names += ($p.ProgrammaticName -replace 'PatternIdentifiers.Pattern$', '' -replace 'Pattern$', '') }
    $names
}
function Describe($e, $i) {
    $c = $e.Current
    $r = $c.BoundingRectangle
    $item = [ordered]@{
        index = $i
        type = $c.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        name = $c.Name
        automation_id = $c.AutomationId
        class = $c.ClassName
        enabled = $c.IsEnabled
        rect = if ($r.IsEmpty) { $null } else { @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) }
        patterns = @(Patterns $e)
    }
    $vp = $null
    if ($e.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { $item.value = $vp.Current.Value }
    $tp = $null
    if ($e.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) { $item.toggle = "$($tp.Current.ToggleState)" }
    $sp = $null
    if ($e.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$sp)) { $item.selected = $sp.Current.IsSelected }
    $item
}

$all = $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
$interesting = @()
foreach ($e in $all) {
    $c = $e.Current
    if ($c.IsOffscreen -and -not $c.Name) { continue }
    $type = $c.ControlType.ProgrammaticName
    $useful = $c.Name -or $c.AutomationId -or ($type -match 'Button|Edit|CheckBox|RadioButton|ComboBox|ListItem|MenuItem|TabItem|TreeItem|Hyperlink|Slider|Document')
    if (-not $useful) { continue }
    $interesting += $e
}

if ($Mode -eq 'inspect') {
    $out = @()
    $i = 0
    foreach ($e in $interesting) {
        $d = Describe $e $i
        $i++
        if ($Filter -and -not ("$($d.name) $($d.automation_id) $($d.type) $($d.value)" -like "*$Filter*")) { continue }
        $out += $d
        if ($out.Count -ge $MaxElements) { break }
    }
    $title = $root.Current.Name
    [ordered]@{ window = $title; count = $interesting.Count; shown = $out.Count; elements = $out } | ConvertTo-Json -Depth 5 -Compress
    exit 0
}

# act: pick the control
$found = @()
$i = 0
foreach ($e in $interesting) {
    $c = $e.Current
    $ok = $true
    if ($Name -and -not ($c.Name -eq $Name -or $c.Name -like $Name -or $c.Name -like "*$Name*")) { $ok = $false }
    if ($AutomationId -and $c.AutomationId -ne $AutomationId) { $ok = $false }
    if ($ControlType -and ($c.ControlType.ProgrammaticName -replace '^ControlType\.', '') -ne $ControlType) { $ok = $false }
    if ($ok) { $found += , @($e, $i) }
    $i++
}
if ($found.Count -eq 0) { throw "no control matches name='$Name' automation_id='$AutomationId' type='$ControlType'" }
if ($Index -ge $found.Count) { throw "only $($found.Count) controls match; index $Index is out of range" }
$target = $found[$Index][0]
$done = $null
$p = $null
switch ($Do) {
    'invoke' {
        if ($target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); $done = 'invoked' }
        elseif ($target.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); $done = 'toggled' }
        elseif ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); $done = 'selected' }
        elseif ($target.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); $done = 'expanded' }
    }
    'toggle' { if ($target.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); $done = 'toggled' } }
    'select' { if ($target.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); $done = 'selected' } }
    'expand' { if ($target.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); $done = 'expanded' } }
    'collapse' { if ($target.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Collapse(); $done = 'collapsed' } }
    'set_value' { if ($target.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { $p.SetValue($Value); $done = 'value set' } }
    'focus' { $target.SetFocus(); $done = 'focused' }
}
# `done` null: the control offers no pattern for this; the caller falls back to a real click
[ordered]@{ done = $done; matches = $found.Count; element = (Describe $target $found[$Index][1]) } | ConvertTo-Json -Depth 5 -Compress
