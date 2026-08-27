# TeamHarness Asset Service Compatibility Test
# Covers: API compat / Response format / Browser UA / Static resources / Encoding
# Usage:  powershell -ExecutionPolicy Bypass -File tests\compat\test_compat.ps1
#         pwsh -File tests\compat\test_compat.ps1

$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"

# Force UTF-8 output (no BOM)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding           = [System.Text.UTF8Encoding]::new($false)

$BaseUrl = "http://localhost:8080"
$Pass = 0; $Fail = 0; $Skip = 0
$Results = [System.Collections.Generic.List[object]]::new()

function Record {
    param([string]$Name, [string]$Status, [string]$Expected, [string]$Actual)
    $script:Results.Add([PSCustomObject]@{
        Name     = $Name
        Status   = $Status
        Expected = $Expected
        Actual   = $Actual
    })
    if ($Status -eq "PASS") { $script:Pass++ }
    elseif ($Status -eq "FAIL") { $script:Fail++ }
    else { $script:Skip++ }
    $color = if ($Status -eq "PASS") { "Green" } elseif ($Status -eq "FAIL") { "Red" } else { "Yellow" }
    $line = "[$Status] $Name"
    if ($Status -ne "PASS" -and $Expected) { $line += "  | Expected: $Expected | Actual: $Actual" }
    Write-Host $line -ForegroundColor $color
}

function Assert-Eq {
    param([string]$Name, $Expected, $Actual)
    if ("$Expected" -eq "$Actual") {
        Record $Name "PASS" "$Expected" "$Actual"
    } else {
        Record $Name "FAIL" "$Expected" "$Actual"
    }
}

function Assert-In {
    param([string]$Name, [array]$ExpectedSet, $Actual)
    if ($ExpectedSet -contains $Actual) {
        Record $Name "PASS" ($ExpectedSet -join "/") "$Actual"
    } else {
        Record $Name "FAIL" ($ExpectedSet -join "/") "$Actual"
    }
}

function Assert-Contains {
    param([string]$Name, [string]$Needle, [string]$Haystack)
    if ($Haystack -and $Haystack.Contains($Needle)) {
        Record $Name "PASS" "contains '$Needle'" "$Haystack"
    } else {
        Record $Name "FAIL" "contains '$Needle'" "$Haystack"
    }
}

# ---------------------------------------------------------------------------
# HTTP request helper (PS 5.1 / 7.x compatible, captures non-2xx without throw)
# ---------------------------------------------------------------------------
function Send-Request {
    param(
        [string]$Uri,
        [string]$Method = "GET",
        [hashtable]$Headers = @{},
        [string]$Body = $null,
        [string]$ContentType = $null,
        [int]$TimeoutSec = 15,
        [int]$MaximumRedirection = -1
    )
    try {
        $params = @{
            Uri             = $Uri
            Method          = $Method
            UseBasicParsing = $true
            TimeoutSec      = $TimeoutSec
            ErrorAction     = "Stop"
        }
        if ($Headers -and $Headers.Count -gt 0) { $params.Headers = $Headers }
        if ($Body) { $params.Body = $Body }
        if ($ContentType) { $params.ContentType = $ContentType }
        if ($MaximumRedirection -ge 0) { $params.MaximumRedirection = $MaximumRedirection }
        $resp = Invoke-WebRequest @params
        $ct = $null
        try { $ct = $resp.Headers["Content-Type"] } catch {}
        # Force UTF-8 decoding (PS 5.1 defaults to ISO-8859-1 when no charset in Content-Type)
        $content = $resp.Content
        try {
            if ($resp.RawContentStream) {
                $rawBytes = $resp.RawContentStream.ToArray()
                $content = [System.Text.Encoding]::UTF8.GetString($rawBytes)
            }
        } catch {}
        return @{
            StatusCode = [int]$resp.StatusCode
            Headers    = $resp.Headers
            Content    = $content
            CT         = $ct
            Error      = $null
        }
    } catch {
        $sc = 0; $ct = $null; $content = ""
        if ($_.Exception.Response) {
            try { $sc = [int]$_.Exception.Response.StatusCode } catch {}
            try {
                if ($_.Exception.Response.Headers -is [System.Net.WebHeaderCollection]) {
                    $ct = $_.Exception.Response.Headers["Content-Type"]
                } elseif ($_.Exception.Response.Headers) {
                    try { $ct = [string]$_.Exception.Response.Content.Headers.ContentType.MediaType } catch {}
                    if (-not $ct) {
                        try { $ct = [string]$_.Exception.Response.Headers."Content-Type" } catch {}
                    }
                }
            } catch {}
        }
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $content = $_.ErrorDetails.Message
        }
        if (-not $content -and $_.Exception.Response) {
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                    $content = $reader.ReadToEnd()
                    $reader.Close()
                }
            } catch {}
        }
        return @{
            StatusCode = $sc
            Headers    = @{ "Content-Type" = $ct }
            Content    = $content
            CT         = $ct
            Error      = $_.Exception.Message
        }
    }
}

# ---------------------------------------------------------------------------
# Step 0: Issue API Key (also serves as reachability probe)
# ---------------------------------------------------------------------------
Write-Host "`n===== Step 0: Service reachability & API Key issuance =====" -ForegroundColor Cyan
$probe = Send-Request -Uri "$BaseUrl/healthz" -Method "GET" -TimeoutSec 5
if ($probe.StatusCode -eq 0) {
    Write-Host "Service $BaseUrl unreachable: $($probe.Error)" -ForegroundColor Red
    exit 1
}

$issueBody = '{"member_id":"alice","agent_id":"agent-alice-compat"}'
$r = Send-Request -Uri "$BaseUrl/v1/auth/apikey" -Method "POST" -Body $issueBody -ContentType "application/json"
if ($r.StatusCode -ne 200) {
    Write-Host "API Key issuance failed: $($r.StatusCode) $($r.Content)" -ForegroundColor Red
    exit 1
}
$apiKeyObj = $r.Content | ConvertFrom-Json
$ApiKey = $apiKeyObj.api_key
Write-Host "API Key issued: $ApiKey`n" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Section 1: Content-Type compatibility
# Test endpoint: POST /v1/auth/apikey/lookup (expects JSON body)
# ---------------------------------------------------------------------------
Write-Host "===== Section 1: Content-Type compatibility =====" -ForegroundColor Cyan
$ctCases = @(
    @{ Name="CT: application/json (standard)"; CT="application/json"; Expected=@(200) },
    @{ Name="CT: application/json; charset=utf-8"; CT="application/json; charset=utf-8"; Expected=@(200) },
    @{ Name="CT: text/plain (expect 422/400)"; CT="text/plain"; Expected=@(422,400) },
    @{ Name="CT: application/x-www-form-urlencoded (expect 422/400)"; CT="application/x-www-form-urlencoded"; Expected=@(422,400) },
    @{ Name="CT: multipart/form-data (expect 422/400)"; CT="multipart/form-data; boundary=----test"; Expected=@(422,400) },
    @{ Name="CT: missing Content-Type (expect 422/400)"; CT=$null; Expected=@(422,400) }
)
foreach ($c in $ctCases) {
    $body = "{`"api_key`":`"$ApiKey`"}"
    $r = Send-Request -Uri "$BaseUrl/v1/auth/apikey/lookup" -Method "POST" -Body $body -ContentType $c.CT
    Assert-In $c.Name $c.Expected $r.StatusCode
}

# ---------------------------------------------------------------------------
# Section 2: HTTP method compatibility
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 2: HTTP method compatibility =====" -ForegroundColor Cyan

# Get a real asset_id for PATCH/DELETE/PUT tests
$r = Send-Request -Uri "$BaseUrl/v1/assets?limit=5" -Method "GET"
$assetsObj = $r.Content | ConvertFrom-Json
$testAssetId = $assetsObj.items[0].id
$testAssetScope = $assetsObj.items[0].scope
Write-Host "Test asset: id=$testAssetId  scope=$testAssetScope" -ForegroundColor Gray

# GET /v1/assets -> 200
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "GET"
Assert-Eq "GET /v1/assets should be 200" 200 $r.StatusCode

# POST /v1/assets -> 405
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "POST" -Body '{}' -ContentType "application/json"
Assert-Eq "POST /v1/assets should be 405 (not implemented)" 405 $r.StatusCode

# PUT /v1/assets/{id}/scope -> 405 (only PATCH supported)
$r = Send-Request -Uri "$BaseUrl/v1/assets/$testAssetId/scope" -Method "PUT" -Body '{"scope":"team"}' -ContentType "application/json"
Assert-Eq "PUT /v1/assets/{id}/scope should be 405" 405 $r.StatusCode

# DELETE /v1/assets/{id} -> 405
$r = Send-Request -Uri "$BaseUrl/v1/assets/$testAssetId" -Method "DELETE"
Assert-Eq "DELETE /v1/assets/{id} should be 405 (not implemented)" 405 $r.StatusCode

# OPTIONS /v1/assets -> 200/204 (CORS preflight)
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "OPTIONS" -Headers @{ "Origin"="http://localhost:8080"; "Access-Control-Request-Method"="GET" }
Assert-In "OPTIONS /v1/assets (CORS preflight) should be 200/204" @(200,204) $r.StatusCode

# HEAD /v1/assets -> 200 or 405
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "HEAD"
Assert-In "HEAD /v1/assets should be 200/405" @(200,405) $r.StatusCode

# PATCH /v1/assets/{id}/scope -> 200 (reuse current scope, idempotent)
$patchBody = "{`"scope`":`"$testAssetScope`"}"
$r = Send-Request -Uri "$BaseUrl/v1/assets/$testAssetId/scope" -Method "PATCH" -Body $patchBody -ContentType "application/json"
Assert-Eq "PATCH /v1/assets/{id}/scope should be 200" 200 $r.StatusCode

# ---------------------------------------------------------------------------
# Section 3: Path variants
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 3: Path variants =====" -ForegroundColor Cyan

# Trailing slash (disable auto-redirect to observe first response)
$r = Send-Request -Uri "$BaseUrl/v1/assets/" -Method "GET" -MaximumRedirection 0
Assert-In "GET /v1/assets/ (trailing slash) should be 200/307/301" @(200,307,301) $r.StatusCode

# Case sensitivity /V1/Assets
$r = Send-Request -Uri "$BaseUrl/V1/Assets" -Method "GET" -MaximumRedirection 0
Assert-In "GET /V1/Assets (case) should be 200/307/404" @(200,307,301,404) $r.StatusCode

# Double slash /v1//assets
$r = Send-Request -Uri "$BaseUrl/v1//assets" -Method "GET" -MaximumRedirection 0
Assert-In "GET /v1//assets (double slash) should be 200/404" @(200,404) $r.StatusCode

# Path traversal
$r = Send-Request -Uri "$BaseUrl/v1/assets/../../../etc/passwd" -Method "GET" -MaximumRedirection 0
Assert-In "GET /v1/assets/../../../etc/passwd (traversal) should be 400/404/422" @(400,404,422) $r.StatusCode

# ---------------------------------------------------------------------------
# Section 4: Response format consistency
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 4: Response format consistency =====" -ForegroundColor Cyan

# Success response is JSON
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "GET"
Assert-Contains "Success response Content-Type should contain application/json" "application/json" ($r.CT)

# Error response (405) is JSON
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "POST" -Body '{}' -ContentType "application/json"
Assert-Contains "405 error response Content-Type should contain application/json" "application/json" ($r.CT)

# Error response body format: {"detail": "xxx"}
$errObj = $null
try { $errObj = $r.Content | ConvertFrom-Json } catch {}
if ($errObj -and $errObj.PSObject.Properties.Name -contains "detail") {
    Record "Error response body should have 'detail' field" "PASS" '{"detail":"xxx"}' $r.Content
} else {
    Record "Error response body should have 'detail' field" "FAIL" '{"detail":"xxx"}' $r.Content
}

# 404: resource not found
$r = Send-Request -Uri "$BaseUrl/v1/assets/non-existent-id-zzz-12345" -Method "GET"
Assert-Eq "GET non-existent asset should be 404" 404 $r.StatusCode
$errObj = $null
try { $errObj = $r.Content | ConvertFrom-Json } catch {}
if ($errObj -and $errObj.PSObject.Properties.Name -contains "detail") {
    Record "404 error response body should have 'detail' field" "PASS" '{"detail":"xxx"}' $r.Content
} else {
    Record "404 error response body should have 'detail' field" "FAIL" '{"detail":"xxx"}' $r.Content
}

# 400: parameter validation failure (invalid scope)
$r = Send-Request -Uri "$BaseUrl/v1/assets/$testAssetId/scope" -Method "PATCH" -Body '{"scope":"invalid_scope_value"}' -ContentType "application/json"
Assert-Eq "PATCH invalid scope should be 400" 400 $r.StatusCode

# 422: request body validation failure (Pydantic, non-JSON body)
$r = Send-Request -Uri "$BaseUrl/v1/auth/apikey/lookup" -Method "POST" -Body 'plain-text-not-json' -ContentType "text/plain"
Assert-In "POST non-JSON body should be 422/400" @(422,400) $r.StatusCode

# 422 detail format (Pydantic error)
$r = Send-Request -Uri "$BaseUrl/v1/auth/apikey/lookup" -Method "POST" -Body '{}' -ContentType "application/json"
Assert-In "POST missing fields should be 422" @(422) $r.StatusCode
$errObj = $null
try { $errObj = $r.Content | ConvertFrom-Json } catch {}
if ($errObj -and $errObj.PSObject.Properties.Name -contains "detail") {
    Record "422 error response body should have 'detail' field" "PASS" '{"detail":[...]}' $r.Content
} else {
    Record "422 error response body should have 'detail' field" "FAIL" '{"detail":[...]}' $r.Content
}

# 500 should not occur (healthy path)
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "GET"
if ($r.StatusCode -eq 500) {
    Record "GET /v1/assets should not return 500" "FAIL" "not 500" "500"
} else {
    Record "GET /v1/assets should not return 500" "PASS" "not 500" "$($r.StatusCode)"
}

# CORS header check (if configured)
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "OPTIONS" -Headers @{ "Origin"="http://example.com"; "Access-Control-Request-Method"="GET" }
$acao = $null
try { $acao = $r.Headers["Access-Control-Allow-Origin"] } catch {}
if ($acao) {
    Record "OPTIONS response has Access-Control-Allow-Origin" "PASS" "present" "$acao"
} else {
    Record "OPTIONS response has Access-Control-Allow-Origin" "SKIP" "present" "CORS not configured (acceptable)"
}

# X-Request-ID header check (if configured)
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "GET"
$xrid = $null
try { $xrid = $r.Headers["X-Request-ID"] } catch {}
if ($xrid) {
    Record "Response has X-Request-ID header" "PASS" "present" "$xrid"
} else {
    Record "Response has X-Request-ID header" "SKIP" "present" "not configured (acceptable)"
}

# ---------------------------------------------------------------------------
# Section 5: Browser User-Agent compatibility
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 5: Browser User-Agent compatibility =====" -ForegroundColor Cyan

$uas = @(
    @{ Label = 'Chrome-Windows';   UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36' },
    @{ Label = 'Edge-Windows';     UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0' },
    @{ Label = 'Firefox-Windows';  UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0' },
    @{ Label = 'Safari-Mac';       UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15' },
    @{ Label = 'Chrome-Android';   UA = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36' },
    @{ Label = 'Safari-iPhone';    UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1' }
)

foreach ($entry in $uas) {
    $label = $entry.Label
    $ua = $entry.UA

    # Frontend HTML page
    $r = Send-Request -Uri "$BaseUrl/" -Method "GET" -Headers @{ "User-Agent" = $ua }
    Assert-Eq "[$label] GET / HTML should be 200" 200 $r.StatusCode

    # API call
    $r = Send-Request -Uri "$BaseUrl/v1/assets?limit=1" -Method "GET" -Headers @{ "User-Agent" = $ua }
    Assert-Eq "[$label] GET /v1/assets API should be 200" 200 $r.StatusCode
}

# ---------------------------------------------------------------------------
# Section 6: Frontend resource loading
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 6: Frontend resource loading =====" -ForegroundColor Cyan

$staticResources = @(
    @{ Path="/"; CTExpect="text/html" },
    @{ Path="/index.html"; CTExpect="text/html" },
    @{ Path="/app.js"; CTExpect="javascript" },
    @{ Path="/services/api.js"; CTExpect="javascript" },
    @{ Path="/services/utils.js"; CTExpect="javascript" }
)
foreach ($res in $staticResources) {
    $r = Send-Request -Uri "$BaseUrl$($res.Path)" -Method "GET"
    Assert-Eq "GET $($res.Path) should be 200" 200 $r.StatusCode
    if ($r.StatusCode -eq 200) {
        Assert-Contains "GET $($res.Path) Content-Type should contain $($res.CTExpect)" $res.CTExpect ($r.CT)
    }
}

# HTML referenced resources have no 404
$indexResp = Send-Request -Uri "$BaseUrl/" -Method "GET"
$indexHtml = $indexResp.Content
# Extract src= and href= attributes using simple regex
$refMatches = [regex]::Matches($indexHtml, '(?:src|href)\s*=\s*"([^"]+)"')
$refs = @()
foreach ($m in $refMatches) {
    $refs += $m.Groups[1].Value
}
$refs = $refs | Sort-Object -Unique
Write-Host "HTML referenced resources: $($refs -join ', ')" -ForegroundColor Gray
foreach ($ref in $refs) {
    if ($ref -match "^https?://") { continue }
    if ($ref -match "^#") { continue }
    # Ensure relative URLs start with /
    if (-not $ref.StartsWith("/")) {
        $url = "$BaseUrl/$ref"
    } else {
        $url = "$BaseUrl$ref"
    }
    $r = Send-Request -Uri $url -Method "GET"
    Assert-Eq "HTML referenced resource '$ref' should be 200" 200 $r.StatusCode
}

# CDN accessibility
Write-Host "`n--- CDN dependency accessibility ---" -ForegroundColor Gray
$cdnUrls = @(
    "https://unpkg.com/vue@3.4.21/dist/vue.global.prod.js",
    "https://unpkg.com/element-plus@2.6.3/dist/index.css",
    "https://unpkg.com/element-plus@2.6.3/dist/index.full.min.js",
    "https://unpkg.com/@element-plus/icons-vue@2.3.1/dist/index.iife.min.js"
)
foreach ($url in $cdnUrls) {
    $r = Send-Request -Uri $url -Method "GET" -TimeoutSec 30
    Assert-Eq "CDN reachable: $url" 200 $r.StatusCode
}

# ---------------------------------------------------------------------------
# Section 7: Viewport / Responsive (requires Playwright, SKIP if unavailable)
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 7: Viewport / Responsive =====" -ForegroundColor Cyan
$hasNpx = $false
try { $cmd = Get-Command npx -ErrorAction Stop; if ($cmd) { $hasNpx = $true } } catch {}
if (-not $hasNpx) {
    Record "Viewport/responsive (multi-resolution)" "SKIP" "Playwright" "npx not available, manual test needed"
} else {
    $pwCheck = & npx --yes playwright --version 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $pwCheck) {
        Record "Viewport/responsive (multi-resolution)" "SKIP" "Playwright" "Playwright not installed, manual test needed"
    } else {
        Record "Viewport/responsive (multi-resolution)" "SKIP" "Playwright" "detected but full render check not integrated in this script"
    }
}

# ---------------------------------------------------------------------------
# Section 8: Encoding and charset
# ---------------------------------------------------------------------------
Write-Host "`n===== Section 8: Encoding and charset =====" -ForegroundColor Cyan

# Chinese asset content (UTF-8)
$r = Send-Request -Uri "$BaseUrl/v1/assets?limit=20&owner=alice" -Method "GET"
$assetsObj = $null
try { $assetsObj = $r.Content | ConvertFrom-Json } catch {}
$hasChinese = $false
$samplePreview = ""
if ($assetsObj -and $assetsObj.items) {
    foreach ($item in $assetsObj.items) {
        # Check for CJK Unified Ideographs range (U+4E00-U+9FFF)
        $hasCJK = $false
        $preview = $item.content_preview
        if ($preview -and $preview.Length -gt 0) {
            foreach ($ch in $preview.ToCharArray()) {
                $c = [int][char]$ch
                if ($c -ge 0x4E00 -and $c -le 0x9FFF) { $hasCJK = $true; break }
            }
        }
        if ($hasCJK) {
            $hasChinese = $true
            $samplePreview = $preview
            break
        }
    }
}
Assert-Eq "Asset content should contain Chinese (UTF-8 OK)" $true $hasChinese

# API response Content-Type should include charset=utf-8
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "GET"
$ct = $r.CT
if ($ct -and $ct -match "charset=utf-8") {
    Record "API response Content-Type should include charset=utf-8" "PASS" "charset=utf-8" "$ct"
} else {
    Record "API response Content-Type should include charset=utf-8" "FAIL" "charset=utf-8" "$ct"
}

# Emoji / Unicode in body can be parsed
# Build emoji body using char codes (surrogate pair for U+1F600)
$emojiHi = [char]0xD83D
$emojiLo = [char]0xDE00
$emoji = "$emojiHi$emojiLo"
$cjkChar = [char]0x4E2D  # zhong
$emojiBody = '{"member_id":"emoji_' + $emoji + '_' + $cjkChar + '","agent_id":"agent-emoji"}'
$r = Send-Request -Uri "$BaseUrl/v1/auth/apikey" -Method "POST" -Body $emojiBody -ContentType "application/json; charset=utf-8"
Assert-In "POST body with emoji/CJK should be parseable (200) or business-validated (422/400)" @(200,422,400) $r.StatusCode

# Error response Chinese detail encoding
# Use a non-existent asset ID that contains CJK to trigger 404 with Chinese detail
$cjkId = [char]0x4E2D + [char]0x6587  # zhong wen
$r = Send-Request -Uri "$BaseUrl/v1/assets/$cjkId" -Method "GET"
$hasChineseInDetail = $false
try {
    $errObj = $r.Content | ConvertFrom-Json
    if ($errObj.detail) {
        foreach ($ch in $errObj.detail.ToCharArray()) {
            $c = [int][char]$ch
            if ($c -ge 0x4E00 -and $c -le 0x9FFF) { $hasChineseInDetail = $true; break }
        }
    }
} catch {}
Assert-Eq "Error response Chinese detail should be UTF-8 OK" $true $hasChineseInDetail

# ---------------------------------------------------------------------------
# Summary & report
# ---------------------------------------------------------------------------
Write-Host "`n===== Summary =====" -ForegroundColor Cyan
$total = $Pass + $Fail + $Skip
Write-Host "PASS: $Pass  /  FAIL: $Fail  /  SKIP: $Skip  /  Total: $total" -ForegroundColor White

# List failed cases
if ($Fail -gt 0) {
    Write-Host "`n--- Failed cases ---" -ForegroundColor Red
    foreach ($r in $Results) {
        if ($r.Status -eq "FAIL") {
            Write-Host "  [FAIL] $($r.Name)" -ForegroundColor Red
            Write-Host "         Expected: $($r.Expected)" -ForegroundColor Gray
            Write-Host "         Actual:   $($r.Actual)" -ForegroundColor Gray
        }
    }
}

# List skipped cases
if ($Skip -gt 0) {
    Write-Host "`n--- Skipped cases ---" -ForegroundColor Yellow
    foreach ($r in $Results) {
        if ($r.Status -eq "SKIP") {
            Write-Host "  [SKIP] $($r.Name) - $($r.Actual)" -ForegroundColor Yellow
        }
    }
}

# Write detailed report file
$reportPath = "d:\Code\TeamHarness\tests\compat\compat_report.txt"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("TeamHarness Asset Service Compatibility Test Report")
$lines.Add("Run time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$lines.Add("BaseUrl: $BaseUrl")
$lines.Add("API Key: $ApiKey")
$lines.Add("=" * 70)
$lines.Add("Summary: PASS=$Pass  FAIL=$Fail  SKIP=$Skip  Total=$total")
$lines.Add("=" * 70)
$lines.Add("")
$lines.Add("===== Detailed results =====")
foreach ($r in $Results) {
    $lines.Add("[$($r.Status)] $($r.Name)")
    if ($r.Expected) { $lines.Add("        Expected: $($r.Expected)") }
    if ($r.Actual)   { $lines.Add("        Actual:   $($r.Actual)") }
}
[System.IO.File]::WriteAllText($reportPath, ($lines -join "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Host "`nDetailed report written to: $reportPath" -ForegroundColor Cyan
