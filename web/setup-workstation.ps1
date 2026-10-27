# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Stefano Noferi
#
# Codendum workstation setup for Windows (prototype). The panel fills in the
# @@...@@ values for one user and serves it once, for
#   curl.exe -fsSk --pinnedpubkey 'sha256//...' https://HOST:8443/s/CODE.ps1 | Out-String | Invoke-Expression
#
# It needs OpenCode 2 and curl.exe 7.58.1 or later (key pinning with Schannel),
# included in up-to-date Windows 10 and 11. It changes only:
# - %USERPROFILE%\.config\codendum\ (certificate, API key, provider snippet);
# - the "codendum" provider and default model in the OpenCode configuration
#   (backup first);
# - NODE_EXTRA_CA_CERTS for the user and the OpenCode service.

& {
    $Base = '@@BASE@@'
    $Pin = '@@PIN@@'
    $Model = '@@MODEL@@'
    $Dir = Join-Path $env:USERPROFILE '.config\codendum'
    $Utf8 = New-Object System.Text.UTF8Encoding $false

    # Native commands: ignore their stderr, keep the exit code.
    function Invoke-Quiet([scriptblock]$Block) {
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $Block 2>&1 | Out-Null } finally { $ErrorActionPreference = $old }
        return $LASTEXITCODE
    }

    try {
        $ErrorActionPreference = 'Stop'
        Write-Host "Codendum: setting up OpenCode for $Base"
        if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw 'curl.exe 7.58.1 or later is required: update Windows' }
        if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
            throw 'OpenCode is not installed. Install it with the commands on the Codendum page, then create a new setup command there.'
        }
        $version = (& opencode --version) -join ' '
        if ($version -notmatch '(\d+)\.' -or [int]$Matches[1] -lt 2) { throw "OpenCode 2 or later is required, found: $version" }
        $conf = & opencode debug paths | Where-Object { $_ -match '^config\s+(.+)$' } |
            ForEach-Object { $Matches[1].Trim() } | Select-Object -First 1
        if (-not $conf) { $conf = Join-Path $env:USERPROFILE '.config\opencode' }

        New-Item -ItemType Directory -Force -Path $Dir, $conf | Out-Null
        $ca = Join-Path $Dir 'ca.pem'
        $keyFile = Join-Path $Dir 'api-key'
        [IO.File]::WriteAllText($ca, @'
@@CA@@
'@, $Utf8)
        [IO.File]::WriteAllText($keyFile, '@@KEY@@', $Utf8)
        $providerText = @'
@@PROVIDER@@
'@
        $provider = $providerText | ConvertFrom-Json
        $provider.settings.apiKey = '{file:' + ($keyFile -replace '\\', '/') + '}'
        [IO.File]::WriteAllText((Join-Path $Dir 'provider.json'), ($provider | ConvertTo-Json -Depth 32), $Utf8)

        Write-Host '- checking the API key with a short request'
        $auth = Join-Path $Dir 'auth.tmp'
        $probe = Join-Path $Dir 'probe.tmp'
        try {
            [IO.File]::WriteAllText($auth, 'Authorization: Bearer ' + [IO.File]::ReadAllText($keyFile) + "`n", $Utf8)
            [IO.File]::WriteAllText($probe, ('{"model":"' + $Model +
                '","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}'), $Utf8)
            $code = & curl.exe -sS -k --pinnedpubkey $Pin --max-time 120 -o NUL -w '%{http_code}' -H "@$auth" `
                -H 'Content-Type: application/json' --data-binary "@$probe" "$Base/v1/chat/completions"
        } finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $auth, $probe
        }
        switch ("$code") {
            '200' { }
            '401' { throw 'the server rejected the API key (HTTP 401)' }
            { $_ -in '502', '503' } { throw "the model server is not ready yet (HTTP $code); try again in a minute" }
            default { throw "cannot reach $Base (HTTP $code)" }
        }

        $file = Join-Path $conf 'opencode.json'
        if (-not (Test-Path $file) -and (Test-Path (Join-Path $conf 'opencode.jsonc'))) { $file = Join-Path $conf 'opencode.jsonc' }
        $cfg = $null
        if (Test-Path $file) {
            $raw = [IO.File]::ReadAllText($file)
            if ($raw.Trim()) {
                try { $cfg = $raw | ConvertFrom-Json } catch {
                    throw "$file is not plain JSON (comments?). Add the provider in $Dir\provider.json to it by hand, under ""providers""."
                }
            }
            Copy-Item $file ($file + '.bak-' + (Get-Date -Format 'yyyyMMddHHmmss'))
            Write-Host "- backup of $file"
        }
        if ($null -eq $cfg) { $cfg = [pscustomobject]@{ '$schema' = 'https://opencode.ai/config.json' } }
        if ($cfg.PSObject.Properties['provider'] -and $cfg.provider.PSObject.Properties['codendum']) {
            throw "$file uses the OpenCode 1.x format"
        }
        if (-not $cfg.PSObject.Properties['providers']) { $cfg | Add-Member -NotePropertyName providers -NotePropertyValue ([pscustomobject]@{}) }
        $cfg.providers | Add-Member -Force -NotePropertyName codendum -NotePropertyValue $provider
        $cfg | Add-Member -Force -NotePropertyName model -NotePropertyValue "codendum/$Model"
        if (-not $cfg.PSObject.Properties['share']) { $cfg | Add-Member -NotePropertyName share -NotePropertyValue 'disabled' }
        [IO.File]::WriteAllText($file, ($cfg | ConvertTo-Json -Depth 64), $Utf8)
        Write-Host "- OpenCode configured: $file (model codendum/$Model)"

        $current = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
        if ($current -and $current -ne $ca) {
            Write-Host "- warning: NODE_EXTRA_CA_CERTS is already set to $current." -ForegroundColor Yellow
            Write-Host "  OpenCode must also trust ${ca}: put both certificates in one file."
        } else {
            [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $ca, 'User')
            $env:NODE_EXTRA_CA_CERTS = $ca
            if ((Invoke-Quiet { opencode service set env NODE_EXTRA_CA_CERTS $ca }) -ne 0) {
                Write-Host '- warning: could not set NODE_EXTRA_CA_CERTS for the OpenCode service' -ForegroundColor Yellow
            }
            Invoke-Quiet { opencode service restart } | Out-Null
            Write-Host '- OpenCode trusts the Codendum certificate'
        }

        Write-Host ''
        Write-Host 'Done. Open a new PowerShell window, go to your project folder and run: opencode' -ForegroundColor Green
    } catch {
        Write-Host ''
        Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
    }
}
