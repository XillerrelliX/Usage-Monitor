# Usage Monitor 1.4.0, Windows x64. Save/review this script before running it.
# Never evaluates downloaded scripts. Only a validated public repository ZIP is downloaded.
[CmdletBinding(DefaultParameterSetName = 'Local')]
param(
    [Parameter(ParameterSetName = 'Local')][string]$SourcePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string]$ArchivePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'GitHub')][string]$Repository,
    [Parameter(ParameterSetName = 'GitHub')][string]$Ref = 'main'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$BundleHash = 'ade8bf937261eeb212f9495749eeeb1d7038f2a541e8c99be062d3e3bcc58f50'
$EngineHash = 'fd6b9ab7f014e5e180c7bab14ee079763c6db07a7cccc48b59ff0da2b12adb37'
$NativeHash = '914f2747432d3395b22e66d4417da9969565d65932b0eb673f62dac4463f0684'
$SourceCommit = '6bd0eed7b108497aa50051580387345340e3167f'
$MaxArchiveBytes = 134217728L
$MaxExpandedBytes = 536870912L
$MaxFileBytes = 134217728L
$workRoot = $null
$skillsRoot = $null
$exitCode = 1

function Get-Sha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Assert-LocalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -notmatch '^[A-Za-z]:[\\/]' -or
        $Path.Substring(2).Contains(':') -or $Path -match '[\x00-\x1f<>"|?*]') {
        throw 'Use an absolute path on a local Windows drive.'
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($full.Length -lt 3) { throw 'A drive root cannot be an installation path.' }
    foreach ($part in $Path.Substring(3).Replace('\', '/').Split('/')) {
        if ($part -eq '.' -or $part -eq '..' -or $part -match '[ .]$' -or
            $part -match '^(CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3]|CONIN\$|CONOUT\$)($|\.)') { throw 'Invalid local Windows path component.' }
    }
    $candidate = $full
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -Force -LiteralPath $candidate
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse points are not allowed in installation paths.' }
            if ($candidate -ne $full -and -not $item.PSIsContainer) { throw 'An installation path ancestor is a file.' }
        }
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if ($parent -eq $candidate) { break }
        $candidate = $parent
    }
    return $full
}

function Assert-RelativeName([string]$Name) {
    if ([string]::IsNullOrEmpty($Name) -or $Name.Contains('\') -or $Name.StartsWith('/') -or
        $Name.Length -gt 220 -or $Name -match '[\x00-\x1f<>:"|?*]') { throw 'Invalid archive or manifest path.' }
    foreach ($part in $Name.Split('/')) {
        if ([string]::IsNullOrEmpty($part) -or $part -eq '.' -or $part -eq '..' -or
            $part -match '[ .]$' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3]|CONIN\$|CONOUT\$)($|\.)') {
            throw ('Invalid archive or manifest path: ' + $Name)
        }
    }
}

function Resolve-InputPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'A local source path is required.' }
    if (-not [IO.Path]::IsPathRooted($Path)) { $Path = Join-Path (Get-Location).ProviderPath $Path }
    return (Assert-LocalPath ([IO.Path]::GetFullPath($Path)))
}

function Get-SafeChild([string]$Root, [string]$Name) {
    Assert-RelativeName $Name
    $path = [IO.Path]::GetFullPath((Join-Path $Root $Name))
    if (-not $path.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase) -or $path.Length -gt 248) {
        throw 'Payload path leaves its root or exceeds supported Windows path length. Use a shorter local path.'
    }
    return $path
}

function Get-RegularFiles([string]$Root) {
    $rootPath = Assert-LocalPath $Root
    $files = New-Object 'System.Collections.Generic.List[string]'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($rootPath)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $folder = Get-Item -Force -LiteralPath $directory
        if (-not $folder.PSIsContainer -or ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Expected a regular payload directory.' }
        foreach ($item in Get-ChildItem -Force -LiteralPath $directory) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Payload reparse points are not allowed.' }
            $name = $item.FullName.Substring($rootPath.Length + 1).Replace('\', '/')
            $null = Get-SafeChild $rootPath $name
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else { $files.Add($name) }
        }
    }
    return ,$files
}

function Assert-FileMap([string]$Root, $Map) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Map.PSObject.Properties) {
        if (-not $names.Add($entry.Name) -or $entry.Value -isnot [string] -or $entry.Value -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid public file hashes.' }
        $path = Get-SafeChild $Root $entry.Name
        $item = Get-Item -Force -LiteralPath $path
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-Sha256 $path) -cne $entry.Value) { throw ('Missing or damaged public payload: ' + $entry.Name) }
    }
    if ($names.Count -eq 0) { throw 'Empty public payload manifest.' }
    return ,$names
}

function Get-NativeSourceHash([string]$Root, $Files) {
    $paths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $null = $paths.Add('native/Cargo.toml')
    $null = $paths.Add('native/Cargo.lock')
    foreach ($name in $Files) {
        if ($name.StartsWith('native/', [StringComparison]::Ordinal) -and
            $name -cmatch '\.(rs|json|toml)$' -and -not $name.Split('/').Contains('target')) { $null = $paths.Add($name) }
    }
    $sorted = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in $paths) { $sorted.Add($name) }
    # pathlib.WindowsPath sorts case-insensitive components, not whole path strings.
    # NUL sorts before every valid component character, preserving that tuple order.
    $comparison = [Comparison[string]] {
        param($left, $right)
        return [StringComparer]::OrdinalIgnoreCase.Compare($left.Replace([char]'/', [char]0), $right.Replace([char]'/', [char]0))
    }
    $sorted.Sort($comparison)
    $bytes = New-Object IO.MemoryStream
    try {
        foreach ($name in $sorted) {
            $nameBytes = [Text.Encoding]::UTF8.GetBytes($name + [char]0)
            $bytes.Write($nameBytes, 0, $nameBytes.Length)
            $hash = Get-Sha256 (Get-SafeChild $Root $name)
            for ($i = 0; $i -lt 64; $i += 2) { $bytes.WriteByte([Convert]::ToByte($hash.Substring($i, 2), 16)) }
        }
        $hash = Get-Sha256 (Get-SafeChild $Root 'assets/runtime-downloads.json')
        for ($i = 0; $i -lt 64; $i += 2) { $bytes.WriteByte([Convert]::ToByte($hash.Substring($i, 2), 16)) }
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($algorithm.ComputeHash($bytes.ToArray()))).Replace('-', '').ToLowerInvariant() }
        finally { $algorithm.Dispose() }
    } finally { $bytes.Dispose() }
}

function Assert-Bundle([string]$Root) {
    $files = Get-RegularFiles $Root
    $manifestPath = Get-SafeChild $Root 'bundle-manifest.json'
    if ((Get-Sha256 $manifestPath) -cne $BundleHash) { throw 'This installer requires the original Usage Monitor 1.4.0 bundle manifest.' }
    $manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $manifest.platform -cne 'windows-x86_64' -or
        $manifest.privateUsageDataIncluded -ne $false -or $manifest.product.name -cne 'Usage Monitor' -or
        $manifest.product.version -cne '1.4.0' -or $manifest.product.engineSha256 -cne $EngineHash -or
        $manifest.product.sourceCommit -cne $SourceCommit) { throw 'Invalid public bundle identity.' }
    $expected = Assert-FileMap $Root $manifest.files
    $null = $expected.Add('bundle-manifest.json')
    if ($files.Count -ne $expected.Count) { throw 'Unexpected or missing public payload files.' }
    foreach ($name in $files) { if (-not $expected.Contains($name)) { throw ('Unexpected public payload: ' + $name) } }
    if ((Get-NativeSourceHash $Root $files) -cne $NativeHash) { throw 'Native source does not match the pinned offline engine.' }
    $runtimeRoot = Join-Path $Root 'runtime'
    $runtime = [IO.File]::ReadAllText((Join-Path $runtimeRoot 'prebuilt-manifest.json')) | ConvertFrom-Json
    if ($runtime.schemaVersion -ne 1 -or $runtime.platform -cne 'windows-x86_64' -or
        $runtime.engineSha256 -cne $EngineHash -or $runtime.nativeSourceHash -cne $NativeHash -or
        $runtime.sourceCommit -cne $SourceCommit) { throw 'Invalid shipped runtime identity.' }
    $runtimeExpected = Assert-FileMap $runtimeRoot $runtime.files
    $null = $runtimeExpected.Add('prebuilt-manifest.json')
    $runtimeFiles = Get-RegularFiles $runtimeRoot
    if ($runtimeFiles.Count -ne $runtimeExpected.Count) { throw 'Unexpected runtime files.' }
    foreach ($name in $runtimeFiles) { if (-not $runtimeExpected.Contains($name)) { throw 'Unexpected runtime file.' } }
    foreach ($name in @('bin/codex-usage-offline.exe', 'python/python.exe', 'python/python314.zip', 'python-packages/tzdata/zoneinfo/UTC', 'prebuilt-required')) {
        if (-not $runtimeExpected.Contains($name)) { throw 'Incomplete shipped runtime.' }
    }
    return ,$files
}

function Expand-Repository([string]$Path, [string]$Target) {
    $Path = Resolve-InputPath $Path
    $file = Get-Item -Force -LiteralPath $Path
    if ($file.PSIsContainer -or $file.Length -gt $MaxArchiveBytes) { throw 'Repository ZIP is missing, invalid or too large.' }
    Add-Type -AssemblyName System.IO.Compression
    $stream = [IO.File]::OpenRead($Path)
    try {
        $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Read, $false)
        try {
            if ($zip.Entries.Count -gt 5000) { throw 'Too many ZIP entries.' }
            $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
            $nodes = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::OrdinalIgnoreCase)
            $records = New-Object 'System.Collections.Generic.List[object]'
            $roots = New-Object 'System.Collections.Generic.List[string]'
            $total = 0L
            foreach ($entry in $zip.Entries) {
                $directory = $entry.FullName.EndsWith('/')
                $name = $entry.FullName
                if ($directory) { $name = $name.Substring(0, $name.Length - 1) }
                Assert-RelativeName $name
                if (-not $names.Add($name)) { throw 'Duplicate Windows ZIP path.' }
                $attributes = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$entry.ExternalAttributes), 0)
                $type = ($attributes -shr 16) -band 61440
                if (($attributes -band 1024) -ne 0 -or ($type -ne 0 -and $type -ne 32768 -and $type -ne 16384) -or
                    ($type -eq 16384 -and -not $directory) -or ($type -eq 32768 -and $directory)) { throw 'ZIP links, reparse points and special entries are forbidden.' }
                if (($directory -and $entry.Length -ne 0) -or $entry.Length -gt $MaxFileBytes) { throw 'Invalid or oversized ZIP entry.' }
                $total += $entry.Length
                if ($total -gt $MaxExpandedBytes -or ($entry.Length -gt 1048576 -and $entry.CompressedLength -gt 0 -and $entry.Length / $entry.CompressedLength -gt 2000)) { throw 'Repository ZIP exceeds extraction bounds.' }
                $parts = $name.Split('/')
                for ($i = 1; $i -le $parts.Length; $i++) {
                    $node = [string]::Join('/', $parts[0..($i - 1)])
                    $kind = 'directory'
                    if ($i -eq $parts.Length -and -not $directory) { $kind = 'file' }
                    if ($nodes.ContainsKey($node) -and ($nodes[$node] -ne $kind)) { throw 'ZIP file/directory path collision.' }
                    $nodes[$node] = $kind
                }
                if (-not $directory -and $name -cmatch '^(?:[^/]+/)?usage-monitor/bundle-manifest\.json$') {
                    $roots.Add($name.Substring(0, $name.Length - 'bundle-manifest.json'.Length))
                }
                $records.Add(@{ Entry = $entry; Name = $name; Directory = $directory })
            }
            if ($roots.Count -ne 1) { throw 'ZIP must contain exactly one repository usage-monitor folder.' }
            $prefix = $roots[0]
            foreach ($record in $records) {
                if ($record.Directory -or -not $record.Name.StartsWith($prefix, [StringComparison]::Ordinal)) { continue }
                $relative = $record.Name.Substring($prefix.Length)
                $destination = Get-SafeChild $Target $relative
                $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                $input = $record.Entry.Open()
                $output = [IO.File]::Open($destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                try {
                    $buffer = New-Object byte[] 65536
                    $written = 0L
                    while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $read
                        if ($written -gt $record.Entry.Length -or $written -gt $MaxFileBytes) { throw 'ZIP entry expanded beyond its declared bounds.' }
                        $output.Write($buffer, 0, $read)
                    }
                    if ($written -ne $record.Entry.Length) { throw 'Truncated ZIP entry.' }
                } finally { $output.Dispose(); $input.Dispose() }
            }
        } finally { $zip.Dispose() }
    } finally { $stream.Dispose() }
}

function Save-GitHubArchive([string]$Name, [string]$Revision, [string]$Target) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$' -or
        $Name.Split('/')[1] -in @('.', '..')) { throw 'Repository must be a public GitHub owner/repository name.' }
    if ([string]::IsNullOrWhiteSpace($Revision) -or $Revision.Length -gt 200 -or $Revision -match '[\x00-\x20\x7f\\?#]' -or
        $Revision.StartsWith('/') -or $Revision.EndsWith('/') -or $Revision -match '(^|/)\.\.?(\/|$)') { throw 'Use a GitHub branch, tag or commit as Ref.' }
    $url = [Uri]('https://codeload.github.com/' + $Name + '/zip/' + [Uri]::EscapeDataString($Revision))
    $oldProtocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $oldProtocol -bor [Net.SecurityProtocolType]::Tls12
        for ($redirect = 0; $redirect -le 5; $redirect++) {
            if ($url.Scheme -cne 'https' -or $url.Host -cnotin @('github.com', 'codeload.github.com') -or $url.Port -ne 443 -or $url.UserInfo -or $url.Fragment) { throw 'GitHub download redirected outside the permitted HTTPS hosts.' }
            $request = [Net.HttpWebRequest]::Create($url)
            $request.AllowAutoRedirect = $false
            $request.Timeout = 60000
            $request.ReadWriteTimeout = 60000
            $request.UserAgent = 'Usage-Monitor-Installer/1.4.0'
            $request.UseDefaultCredentials = $false
            $response = $request.GetResponse()
            try {
                if ([int]$response.StatusCode -in @(301, 302, 303, 307, 308)) {
                    if ($redirect -eq 5 -or -not $response.Headers['Location']) { throw 'Too many or invalid GitHub redirects.' }
                    $url = New-Object Uri($url, $response.Headers['Location'])
                    continue
                }
                if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt $MaxArchiveBytes) { throw 'GitHub archive download failed or exceeds its size limit.' }
                $input = $response.GetResponseStream()
                $output = [IO.File]::Open($Target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                try {
                    $buffer = New-Object byte[] 65536
                    $written = 0L
                    while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $written += $read
                        if ($written -gt $MaxArchiveBytes) { throw 'GitHub archive exceeds its download limit.' }
                        $output.Write($buffer, 0, $read)
                    }
                } finally { $output.Dispose(); $input.Dispose() }
                return
            } finally { $response.Dispose() }
        }
    } finally { [Net.ServicePointManager]::SecurityProtocol = $oldProtocol }
}

function Remove-OwnedWork([string]$Path, [string]$Parent) {
    $checked = Assert-LocalPath $Path
    $checkedParent = Assert-LocalPath $Parent
    if ([IO.Path]::GetDirectoryName($checked) -cne $checkedParent -or
        [IO.Path]::GetFileName($checked) -cnotmatch '^\.usage-monitor-install-[0-9a-f]{32}$') { throw 'Refusing cleanup outside the exact installer staging directory.' }
    $null = Get-RegularFiles $checked
    Remove-Item -Force -Recurse -LiteralPath $checked
}

try {
    if ($env:OS -cne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem -or
        $env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'x86') -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { throw 'Usage Monitor requires Windows x64.' }
    if ($PSVersionTable.PSVersion -lt [Version]'5.1') { throw 'Windows PowerShell 5.1 or newer is required.' }
    if ($env:CODEX_HOME) { $codexRoot = Assert-LocalPath $env:CODEX_HOME }
    elseif ($env:USERPROFILE) { $codexRoot = Assert-LocalPath (Join-Path $env:USERPROFILE '.codex') }
    else { throw 'USERPROFILE or CODEX_HOME must identify a local user folder.' }
    $skillsRoot = Assert-LocalPath (Join-Path $codexRoot 'skills')
    $destination = Assert-LocalPath (Join-Path $skillsRoot 'usage-monitor')
    $existing = Test-Path -LiteralPath $destination
    if ($existing) {
        $null = Assert-Bundle $destination
        Write-Output ('Usage Monitor 1.4.0 is already installed and verified: ' + $destination)
        $exitCode = 0
    } else {
        $null = [IO.Directory]::CreateDirectory($skillsRoot)
        $null = Assert-LocalPath $skillsRoot
        $workRoot = Join-Path $skillsRoot ('.usage-monitor-install-' + [Guid]::NewGuid().ToString('N'))
        $null = [IO.Directory]::CreateDirectory($workRoot)
        $stage = Join-Path $workRoot 'payload'
        $null = [IO.Directory]::CreateDirectory($stage)
        if ($PSCmdlet.ParameterSetName -eq 'GitHub') {
            $download = Join-Path $workRoot 'repository.zip'
            Save-GitHubArchive $Repository $Ref $download
            Expand-Repository $download $stage
        } elseif ($PSCmdlet.ParameterSetName -eq 'Archive') {
            Expand-Repository $ArchivePath $stage
        } else {
            if (-not $SourcePath) { $SourcePath = $PSScriptRoot }
            $repositoryRoot = Resolve-InputPath $SourcePath
            $source = Assert-LocalPath (Join-Path $repositoryRoot 'usage-monitor')
            $sourceFiles = Assert-Bundle $source
            foreach ($name in $sourceFiles) {
                $target = Get-SafeChild $stage $name
                $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
                [IO.File]::Copy((Get-SafeChild $source $name), $target, $false)
            }
        }
        $null = Assert-Bundle $stage
        # Only help is executed: it cannot scan session files or create a usage report.
        $launcher = Join-Path $stage 'Start-Usage-Monitor.cmd'
        & $launcher --help
        $helpExit = $LASTEXITCODE
        if ($helpExit -notin @(0, 2)) { throw 'The verified launcher help check failed; installation was not published.' }
        $null = Assert-Bundle $stage
        $null = Assert-LocalPath $skillsRoot
        if (Test-Path -LiteralPath $destination) { throw 'Destination appeared during installation; no files were overwritten.' }
        [IO.Directory]::Move($stage, $destination)
        Write-Output ('Installed Usage Monitor 1.4.0: ' + $destination)
        Write-Output 'Restart Codex to discover $usage-monitor, or open Start-Usage-Monitor.cmd directly.'
        $exitCode = $helpExit
    }
} catch {
    [Console]::Error.WriteLine('Usage Monitor installation failed: ' + $_.Exception.Message)
    [Console]::Error.WriteLine('Existing skills and personal usage data are preserved. Use a clean destination; this installer never updates or removes an existing skill.')
    $exitCode = 1
} finally {
    if ($workRoot -and (Test-Path -LiteralPath $workRoot)) {
        try { Remove-OwnedWork $workRoot $skillsRoot }
        catch { [Console]::Error.WriteLine('Installer staging cleanup needs attention: ' + $_.Exception.Message); if ($exitCode -eq 0) { $exitCode = 2 } }
    }
}
exit $exitCode
