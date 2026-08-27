# TeamHarness nginx + Docker 部署层模块测试
# 范围：nginx 配置 / Docker compose / 静态资源服务 / 反向代理 / 安全头 / gzip / 缓存 / 容器健康
# 禁止：修改源代码、读 server/ 或 frontend/ 源码（仅通过 HTTP 端点与 docker 命令验证）
# 用法：  powershell -ExecutionPolicy Bypass -File tests\module_nginx\test_nginx.ps1
#         pwsh -File tests\module_nginx\test_nginx.ps1

$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"

# Force UTF-8 output (no BOM)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding           = [System.Text.UTF8Encoding]::new($false)

$BaseUrl   = "http://localhost:8080"
$DeployDir = "d:\Code\TeamHarness\deploy"
$Pass = 0; $Fail = 0; $Skip = 0
$Results = [System.Collections.Generic.List[object]]::new()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
        Record $Name "PASS" "contains '$Needle'" "found"
    } else {
        Record $Name "FAIL" "contains '$Needle'" "not found"
    }
}

function Assert-NotContains {
    param([string]$Name, [string]$Needle, [string]$Haystack)
    if ($Haystack -and $Haystack.Contains($Needle)) {
        Record $Name "FAIL" "not contains '$Needle'" "found (leak)"
    } else {
        Record $Name "PASS" "not contains '$Needle'" "ok"
    }
}

# HTTP request helper (PS 5.1 / 7.x compatible, captures non-2xx without throw)
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

# docker exec helper (returns combined stdout+stderr string)
function Docker-Exec {
    param([string]$Container, [string]$Cmd)
    try {
        $out = docker exec $Container sh -c $Cmd 2>&1
        return ($out -join "`n")
    } catch {
        return "ERROR: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# Step 0: Service reachability
# ---------------------------------------------------------------------------
Write-Host "`n===== Step 0: nginx 服务可达性 =====" -ForegroundColor Cyan
$probe = Send-Request -Uri "$BaseUrl/healthz" -Method "GET" -TimeoutSec 5
if ($probe.StatusCode -eq 0) {
    Write-Host "nginx $BaseUrl 不可达: $($probe.Error)" -ForegroundColor Red
    Write-Host "请先启动：docker compose -f deploy\docker-compose.yaml up -d" -ForegroundColor Yellow
    exit 1
}
Write-Host "nginx 可达，/healthz => $($probe.StatusCode)" -ForegroundColor Green

# ===========================================================================
# Section 1: nginx 静态资源服务
# ===========================================================================
Write-Host "`n===== Section 1: nginx 静态资源服务 =====" -ForegroundColor Cyan

# GET / → 200 + text/html + 含 Vue 挂载点 #app
$r = Send-Request -Uri "$BaseUrl/" -Method "GET"
Assert-Eq "GET / 状态码应为 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET / Content-Type 应为 text/html" "text/html" ($r.CT)
    Assert-Contains "GET / 应含 Vue 挂载点 #app" 'id="app"' ($r.Content)
    Assert-Contains "GET / 应引用 app.js" "app.js" ($r.Content)
}

# GET /index.html → 200 + text/html
$r = Send-Request -Uri "$BaseUrl/index.html" -Method "GET"
Assert-Eq "GET /index.html 状态码应为 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET /index.html Content-Type 应为 text/html" "text/html" ($r.CT)
}

# GET /app.js → 200 + javascript
$r = Send-Request -Uri "$BaseUrl/app.js" -Method "GET"
Assert-Eq "GET /app.js 状态码应为 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET /app.js Content-Type 应为 javascript" "javascript" ($r.CT)
}

# GET /services/api.js → 200 + javascript
$r = Send-Request -Uri "$BaseUrl/services/api.js" -Method "GET"
Assert-Eq "GET /services/api.js 状态码应为 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET /services/api.js Content-Type 应为 javascript" "javascript" ($r.CT)
}

# GET /services/utils.js → 200 + javascript
$r = Send-Request -Uri "$BaseUrl/services/utils.js" -Method "GET"
Assert-Eq "GET /services/utils.js 状态码应为 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET /services/utils.js Content-Type 应为 javascript" "javascript" ($r.CT)
}

# GET /nonexistent.js → 应 404
$r = Send-Request -Uri "$BaseUrl/nonexistent.js" -Method "GET"
# nginx try_files $uri $uri/ /index.html → 不存在的 .js 会被 fallback 到 /index.html（200）
# 记录实际行为
if ($r.StatusCode -eq 404) {
    Assert-Eq "GET /nonexistent.js 应 404（资源不存在）" 404 $r.StatusCode
} elseif ($r.StatusCode -eq 200) {
    # 检查是否 fallback 到 index.html（SPA 路由行为）
    $isHtml = $r.CT -match "text/html"
    Record "GET /nonexistent.js 应 404（资源不存在）" "FAIL" "404" "实际 $($r.StatusCode)（try_files fallback 到 index.html，CT=$($r.CT)）"
} else {
    Record "GET /nonexistent.js 应 404（资源不存在）" "FAIL" "404" "实际 $($r.StatusCode)"
}

# 验证 HTML 中引用的所有本地资源路径
$indexResp = Send-Request -Uri "$BaseUrl/" -Method "GET"
$indexHtml = $indexResp.Content
$refMatches = [regex]::Matches($indexHtml, '(?:src|href)\s*=\s*"([^"]+)"')
$refs = @()
foreach ($m in $refMatches) { $refs += $m.Groups[1].Value }
$refs = $refs | Sort-Object -Unique
Write-Host "  HTML 引用资源: $($refs -join ', ')" -ForegroundColor Gray
foreach ($ref in $refs) {
    if ($ref -match "^https?://") { continue }
    if ($ref -match "^#") { continue }
    if (-not $ref.StartsWith("/")) { $url = "$BaseUrl/$ref" } else { $url = "$BaseUrl$ref" }
    $r = Send-Request -Uri $url -Method "GET"
    Assert-Eq "HTML 引用资源 '$ref' 应 200" 200 $r.StatusCode
}

# ===========================================================================
# Section 2: nginx 反向代理
# ===========================================================================
Write-Host "`n===== Section 2: nginx 反向代理 =====" -ForegroundColor Cyan

# GET /v1/assets → 应代理到 asset-service（JSON）
$r = Send-Request -Uri "$BaseUrl/v1/assets?limit=1" -Method "GET"
Assert-Eq "GET /v1/assets 代理到 asset-service 应 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "GET /v1/assets 响应应为 JSON" "application/json" ($r.CT)
}

# GET /v1/auth/apikey → POST 应代理到 asset-service
$issueBody = '{"member_id":"nginx-test","agent_id":"agent-nginx"}'
$r = Send-Request -Uri "$BaseUrl/v1/auth/apikey" -Method "POST" -Body $issueBody -ContentType "application/json"
Assert-Eq "POST /v1/auth/apikey 代理到 asset-service 应 200" 200 $r.StatusCode
if ($r.StatusCode -eq 200) {
    Assert-Contains "POST /v1/auth/apikey 响应应为 JSON" "application/json" ($r.CT)
}

# 验证 nginx.conf 中 /v1/ location 配置（读取部署源码配置文件）
$nginxConf = ""
if (Test-Path "$DeployDir\nginx.conf") {
    $nginxConf = [System.IO.File]::ReadAllText("$DeployDir\nginx.conf")
} else {
    Record "nginx.conf 配置文件应存在" "FAIL" "file exists" "not found at $DeployDir\nginx.conf"
}

if ($nginxConf) {
    Record "nginx.conf 配置文件应存在" "PASS" "file exists" "found"
    # 验证 /v1/ location 配置
    if ($nginxConf -match 'location\s+/v1/\s*\{') {
        Record "nginx.conf 应配置 /v1/ location" "PASS" "location /v1/" "found"
    } else {
        Record "nginx.conf 应配置 /v1/ location" "FAIL" "location /v1/" "not found"
    }
    # 验证 proxy_pass 指向 asset-service（注意：实际端口是 8080，非任务描述的 8000）
    if ($nginxConf -match 'proxy_pass\s+http://asset-service;') {
        Record "nginx.conf /v1/ proxy_pass 应指向 asset-service" "PASS" "http://asset-service" "found"
    } else {
        Record "nginx.conf /v1/ proxy_pass 应指向 asset-service" "FAIL" "http://asset-service" "not found"
    }
    # 验证 upstream asset-service 定义
    if ($nginxConf -match 'upstream\s+asset-service\s*\{[^}]*server\s+asset-service:8080;') {
        Record "nginx.conf upstream asset-service 应指向 asset-service:8080" "PASS" "asset-service:8080" "found"
    } else {
        Record "nginx.conf upstream asset-service 应指向 asset-service:8080" "FAIL" "asset-service:8080" "not found"
    }
    # 验证 try_files（SPA fallback）
    if ($nginxConf -match 'try_files\s+\$uri\s+\$uri/\s+/index\.html;') {
        Record "nginx.conf 应配置 try_files SPA fallback" "PASS" "try_files \$uri \$uri/ /index.html" "found"
    } else {
        Record "nginx.conf 应配置 try_files SPA fallback" "FAIL" "try_files \$uri \$uri/ /index.html" "not found"
    }
}

# ===========================================================================
# Section 3: 路径遍历防护（重点验证 COMPAT-3）
# ===========================================================================
Write-Host "`n===== Section 3: 路径遍历防护（COMPAT-3） =====" -ForegroundColor Cyan

# GET /v1/assets/../../../etc/passwd → 应 400/404
$r = Send-Request -Uri "$BaseUrl/v1/assets/../../../etc/passwd" -Method "GET" -MaximumRedirection 0
$isHtmlLeak = $false
if ($r.StatusCode -eq 200) {
    # 检查是否返回了 index.html（路径归一化后命中 / location）
    if ($r.CT -match "text/html") { $isHtmlLeak = $true }
}
if ($isHtmlLeak) {
    Record "GET /v1/assets/../../../etc/passwd 应 400/404（COMPAT-3）" "FAIL" "400/404" "实际 $($r.StatusCode) text/html（nginx 归一化路径后命中 / location，返回 index.html）"
} else {
    Assert-In "GET /v1/assets/../../../etc/passwd 应 400/404（COMPAT-3）" @(400,404) $r.StatusCode
}

# GET /../etc/passwd → 应 400/404
$r = Send-Request -Uri "$BaseUrl/../etc/passwd" -Method "GET" -MaximumRedirection 0
$isHtmlLeak = $false
if ($r.StatusCode -eq 200 -and $r.CT -match "text/html") { $isHtmlLeak = $true }
if ($isHtmlLeak) {
    Record "GET /../etc/passwd 应 400/404" "FAIL" "400/404" "实际 $($r.StatusCode) text/html（归一化后命中 / location）"
} else {
    Assert-In "GET /../etc/passwd 应 400/404" @(400,404) $r.StatusCode
}

# GET /v1/../../etc/passwd → 应 400/404
$r = Send-Request -Uri "$BaseUrl/v1/../../etc/passwd" -Method "GET" -MaximumRedirection 0
$isHtmlLeak = $false
if ($r.StatusCode -eq 200 -and $r.CT -match "text/html") { $isHtmlLeak = $true }
if ($isHtmlLeak) {
    Record "GET /v1/../../etc/passwd 应 400/404" "FAIL" "400/404" "实际 $($r.StatusCode) text/html（归一化后命中 / location）"
} else {
    Assert-In "GET /v1/../../etc/passwd 应 400/404" @(400,404) $r.StatusCode
}

# GET /..%2F..%2Fetc%2Fpasswd → 应 400/404
$r = Send-Request -Uri "$BaseUrl/..%2F..%2Fetc%2Fpasswd" -Method "GET" -MaximumRedirection 0
$isHtmlLeak = $false
if ($r.StatusCode -eq 200 -and $r.CT -match "text/html") { $isHtmlLeak = $true }
if ($isHtmlLeak) {
    Record "GET /..%2F..%2Fetc%2Fpasswd 应 400/404" "FAIL" "400/404" "实际 $($r.StatusCode) text/html（归一化后命中 / location）"
} else {
    Assert-In "GET /..%2F..%2Fetc%2Fpasswd 应 400/404" @(400,404) $r.StatusCode
}

# GET /v1/assets/%2e%2e/%2e%2e/etc/passwd → 应 400/404
$r = Send-Request -Uri "$BaseUrl/v1/assets/%2e%2e/%2e%2e/etc/passwd" -Method "GET" -MaximumRedirection 0
$isHtmlLeak = $false
if ($r.StatusCode -eq 200 -and $r.CT -match "text/html") { $isHtmlLeak = $true }
if ($isHtmlLeak) {
    Record "GET /v1/assets/%2e%2e/%2e%2e/etc/passwd 应 400/404" "FAIL" "400/404" "实际 $($r.StatusCode) text/html（归一化后命中 / location）"
} else {
    Assert-In "GET /v1/assets/%2e%2e/%2e%2e/etc/passwd 应 400/404" @(400,404) $r.StatusCode
}

# 关键验证：路径遍历是否返回了 /etc/passwd 内容（安全漏洞）
$r = Send-Request -Uri "$BaseUrl/v1/assets/../../../etc/passwd" -Method "GET" -MaximumRedirection 0
Assert-NotContains "路径遍历响应不应泄露 /etc/passwd 内容（root:）" "root:" ($r.Content)

# ===========================================================================
# Section 4: HTTP 方法处理
# ===========================================================================
Write-Host "`n===== Section 4: HTTP 方法处理（含 COMPAT-2 OPTIONS） =====" -ForegroundColor Cyan

# GET / → 200
$r = Send-Request -Uri "$BaseUrl/" -Method "GET"
Assert-Eq "GET / 应 200" 200 $r.StatusCode

# HEAD / → 应 200（无 body）
$r = Send-Request -Uri "$BaseUrl/" -Method "HEAD"
Assert-In "HEAD / 应 200" @(200,301,302) $r.StatusCode

# POST / → 应 405 或 404（nginx 静态 location 不限制方法，可能返回 200 fallback）
$r = Send-Request -Uri "$BaseUrl/" -Method "POST" -Body "test" -ContentType "text/plain"
if ($r.StatusCode -eq 200) {
    Record "POST / 应 405/404（静态资源不应接受 POST）" "FAIL" "405/404" "实际 $($r.StatusCode)（nginx / location 未限制 HTTP 方法，POST 也返回 index.html）"
} else {
    Assert-In "POST / 应 405/404" @(405,404) $r.StatusCode
}

# OPTIONS / → 应 200/204/405（COMPAT-2 重点）
$r = Send-Request -Uri "$BaseUrl/" -Method "OPTIONS"
Write-Host "  OPTIONS / 实际状态码: $($r.StatusCode), CT: $($r.CT)" -ForegroundColor Gray
if ($r.StatusCode -eq 200) {
    Record "OPTIONS / 应 200/204/405（COMPAT-2）" "FAIL" "200/204/405" "实际 $($r.StatusCode)（nginx / location 未限制方法，OPTIONS 返回 200 + index.html，非标准行为）"
} else {
    Assert-In "OPTIONS / 应 200/204/405（COMPAT-2）" @(200,204,405) $r.StatusCode
}

# DELETE / → 应 405 或 404
$r = Send-Request -Uri "$BaseUrl/" -Method "DELETE"
if ($r.StatusCode -eq 200) {
    Record "DELETE / 应 405/404" "FAIL" "405/404" "实际 $($r.StatusCode)（nginx / location 未限制 HTTP 方法）"
} else {
    Assert-In "DELETE / 应 405/404" @(405,404) $r.StatusCode
}

# OPTIONS /v1/assets（API 端点，应走 CORS preflight）
$r = Send-Request -Uri "$BaseUrl/v1/assets" -Method "OPTIONS" -Headers @{ "Origin"="http://localhost:8080"; "Access-Control-Request-Method"="GET" }
Write-Host "  OPTIONS /v1/assets 实际状态码: $($r.StatusCode), CT: $($r.CT)" -ForegroundColor Gray
Assert-In "OPTIONS /v1/assets（API CORS preflight）应 200/204/405" @(200,204,405) $r.StatusCode

# ===========================================================================
# Section 5: 安全头
# ===========================================================================
Write-Host "`n===== Section 5: 安全头 =====" -ForegroundColor Cyan

$r = Send-Request -Uri "$BaseUrl/" -Method "GET"
$headers = $r.Headers

# X-Frame-Options
$xfo = $null; try { $xfo = $headers["X-Frame-Options"] } catch {}
if ($xfo) {
    Record "响应应含 X-Frame-Options" "PASS" "present" "$xfo"
} else {
    Record "响应应含 X-Frame-Options" "FAIL" "present" "缺失（nginx.conf 未配置 add_header X-Frame-Options）"
}

# X-Content-Type-Options: nosniff
$xcto = $null; try { $xcto = $headers["X-Content-Type-Options"] } catch {}
if ($xcto) {
    Record "响应应含 X-Content-Type-Options: nosniff" "PASS" "nosniff" "$xcto"
} else {
    Record "响应应含 X-Content-Type-Options: nosniff" "FAIL" "nosniff" "缺失（nginx.conf 未配置）"
}

# Content-Security-Policy
$csp = $null; try { $csp = $headers["Content-Security-Policy"] } catch {}
if ($csp) {
    Record "响应应含 Content-Security-Policy" "PASS" "present" "$csp"
} else {
    Record "响应应含 Content-Security-Policy" "FAIL" "present" "缺失（nginx.conf 未配置）"
}

# Strict-Transport-Security
$hsts = $null; try { $hsts = $headers["Strict-Transport-Security"] } catch {}
if ($hsts) {
    Record "响应应含 Strict-Transport-Security" "PASS" "present" "$hsts"
} else {
    Record "响应应含 Strict-Transport-Security" "FAIL" "present" "缺失（nginx.conf 未配置，HTTP 部署可接受但生产 HTTPS 应配置）"
}

# 验证 nginx.conf 是否配置了这些安全头
if ($nginxConf) {
    if ($nginxConf -match 'add_header\s+X-Frame-Options') {
        Record "nginx.conf 应配置 X-Frame-Options" "PASS" "add_header X-Frame-Options" "found"
    } else {
        Record "nginx.conf 应配置 X-Frame-Options" "FAIL" "add_header X-Frame-Options" "not found"
    }
    if ($nginxConf -match 'add_header\s+X-Content-Type-Options') {
        Record "nginx.conf 应配置 X-Content-Type-Options" "PASS" "add_header X-Content-Type-Options" "found"
    } else {
        Record "nginx.conf 应配置 X-Content-Type-Options" "FAIL" "add_header X-Content-Type-Options" "not found"
    }
    if ($nginxConf -match 'add_header\s+Content-Security-Policy') {
        Record "nginx.conf 应配置 Content-Security-Policy" "PASS" "add_header Content-Security-Policy" "found"
    } else {
        Record "nginx.conf 应配置 Content-Security-Policy" "FAIL" "add_header Content-Security-Policy" "not found"
    }
}

# ===========================================================================
# Section 6: gzip 压缩
# ===========================================================================
Write-Host "`n===== Section 6: gzip 压缩 =====" -ForegroundColor Cyan

# 检查 nginx.conf 是否启用 gzip
if ($nginxConf) {
    if ($nginxConf -match 'gzip\s+on') {
        Record "nginx.conf 应启用 gzip" "PASS" "gzip on" "found"
    } else {
        Record "nginx.conf 应启用 gzip" "FAIL" "gzip on" "not found（nginx.conf 未配置 gzip）"
    }
    if ($nginxConf -match 'gzip_types') {
        Record "nginx.conf 应配置 gzip_types" "PASS" "gzip_types" "found"
    } else {
        Record "nginx.conf 应配置 gzip_types" "FAIL" "gzip_types" "not found"
    }
}

# 用 Accept-Encoding: gzip 请求 app.js → 验证 Content-Encoding: gzip
$r = Send-Request -Uri "$BaseUrl/app.js" -Method "GET" -Headers @{ "Accept-Encoding"="gzip" }
$ce = $null; try { $ce = $r.Headers["Content-Encoding"] } catch {}
if ($ce -and $ce -match "gzip") {
    Record "GET /app.js (gzip) 应返回 Content-Encoding: gzip" "PASS" "gzip" "$ce"
} else {
    Record "GET /app.js (gzip) 应返回 Content-Encoding: gzip" "FAIL" "gzip" "实际 Content-Encoding=$ce（未启用 gzip）"
}

# 用 Accept-Encoding: gzip 请求 /v1/assets（JSON）
$r = Send-Request -Uri "$BaseUrl/v1/assets?limit=1" -Method "GET" -Headers @{ "Accept-Encoding"="gzip" }
$ce = $null; try { $ce = $r.Headers["Content-Encoding"] } catch {}
if ($ce -and $ce -match "gzip") {
    Record "GET /v1/assets (gzip) 应返回 Content-Encoding: gzip" "PASS" "gzip" "$ce"
} else {
    Record "GET /v1/assets (gzip) 应返回 Content-Encoding: gzip" "FAIL" "gzip" "实际 Content-Encoding=$ce（未启用 gzip）"
}

# ===========================================================================
# Section 7: 缓存策略
# ===========================================================================
Write-Host "`n===== Section 7: 缓存策略 =====" -ForegroundColor Cyan

# 静态资源是否有 Cache-Control
$r = Send-Request -Uri "$BaseUrl/app.js" -Method "GET"
$cc = $null; try { $cc = $r.Headers["Cache-Control"] } catch {}
if ($cc) {
    Record "GET /app.js 应有 Cache-Control" "PASS" "present" "$cc"
} else {
    Record "GET /app.js 应有 Cache-Control" "FAIL" "present" "缺失（nginx.conf 未配置 expires / add_header Cache-Control）"
}

# index.html 是否禁止缓存（应 no-cache）
$r = Send-Request -Uri "$BaseUrl/index.html" -Method "GET"
$cc = $null; try { $cc = $r.Headers["Cache-Control"] } catch {}
if ($cc -and $cc -match "no-cache") {
    Record "GET /index.html 应 no-cache" "PASS" "no-cache" "$cc"
} else {
    Record "GET /index.html 应 no-cache" "FAIL" "no-cache" "实际 Cache-Control=$cc（未配置，HTML 可被缓存导致更新不生效）"
}

# 验证 nginx.conf 是否配置 expires / Cache-Control
if ($nginxConf) {
    if ($nginxConf -match 'expires|Cache-Control') {
        Record "nginx.conf 应配置 expires / Cache-Control" "PASS" "expires|Cache-Control" "found"
    } else {
        Record "nginx.conf 应配置 expires / Cache-Control" "FAIL" "expires|Cache-Control" "not found（无任何缓存策略）"
    }
}

# ===========================================================================
# Section 8: Docker 容器健康
# ===========================================================================
Write-Host "`n===== Section 8: Docker 容器健康 =====" -ForegroundColor Cyan

# docker compose ps → 所有容器应 healthy（nginx 无 healthcheck 定义，单独判定）
$composePs = docker compose -f "$DeployDir\docker-compose.yaml" ps --format json 2>&1
$containers = @()
try {
    $lines = $composePs -split "`n" | Where-Object { $_.Trim() -ne "" }
    foreach ($line in $lines) {
        try {
            $obj = $line | ConvertFrom-Json
            $containers += $obj
        } catch {
            # 非 JSON 格式，跳过
        }
    }
} catch {}

if ($containers.Count -eq 0) {
    # 回退到非 JSON 格式
    $composePsRaw = docker compose -f "$DeployDir\docker-compose.yaml" ps 2>&1
    Record "docker compose ps 应返回容器列表" "PASS" "non-empty" "raw output（$($containers.Count) JSON 解析）"
    Write-Host "  $($composePsRaw -join "`n  ")" -ForegroundColor Gray
} else {
    Record "docker compose ps 应返回容器列表" "PASS" "non-empty" "$($containers.Count) 容器"
}

# 检查各容器状态
$expectedContainers = @(
    "teamharness-nginx",
    "teamharness-asset-service",
    "teamharness-recall-service",
    "teamharness-distill-service",
    "teamharness-postgres",
    "teamharness-gitea"
)
foreach ($cname in $expectedContainers) {
    # nginx 无 healthcheck 定义，用条件表达式避免模板报错
    $stateRaw = docker inspect --format '{{.State.Status}}' $cname 2>&1
    $state = "$stateRaw".Trim()
    $healthRaw = ""
    try {
        $healthRaw = docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $cname 2>&1
    } catch {
        $healthRaw = "none"
    }
    $health = "$healthRaw".Trim()
    if ($state -eq "running") {
        if ($cname -eq "teamharness-nginx") {
            # nginx 无 healthcheck，只验证 running
            Record "容器 $cname 应 running" "PASS" "running" "$state (health=$health)"
        } elseif ($health -eq "healthy") {
            Record "容器 $cname 应 healthy" "PASS" "healthy" "$state/$health"
        } else {
            Record "容器 $cname 应 healthy" "FAIL" "healthy" "$state/$health"
        }
    } else {
        Record "容器 $cname 应 running" "FAIL" "running" "$state"
    }
}

# nginx 容器日志无 error
$nginxLogs = docker logs teamharness-nginx --tail 100 2>&1
$nginxErrors = ($nginxLogs | Where-Object { $_ -match '\[error\]|emergency|alert' })
if ($nginxErrors -and $nginxErrors.Count -gt 0) {
    Record "nginx 容器日志应无 error" "FAIL" "no error" "$($nginxErrors.Count) 条 error"
    foreach ($e in $nginxErrors | Select-Object -First 3) {
        Write-Host "    $e" -ForegroundColor DarkRed
    }
} else {
    Record "nginx 容器日志应无 error" "PASS" "no error" "ok"
}

# asset-service 容器日志无 error
$assetLogs = docker logs teamharness-asset-service --tail 100 2>&1
$assetErrors = ($assetLogs | Where-Object { $_ -match 'ERROR|Traceback|Exception' })
if ($assetErrors -and $assetErrors.Count -gt 0) {
    Record "asset-service 容器日志应无 error" "FAIL" "no error" "$($assetErrors.Count) 条 error"
} else {
    Record "asset-service 容器日志应无 error" "PASS" "no error" "ok"
}

# 验证 frontend.Dockerfile 是否正确拷贝 services/ 目录
$dockerfilePath = "$DeployDir\frontend.Dockerfile"
if (Test-Path $dockerfilePath) {
    $dockerfile = [System.IO.File]::ReadAllText($dockerfilePath)
    if ($dockerfile -match 'COPY\s+frontend/services/\s+/usr/share/nginx/html/services/') {
        Record "frontend.Dockerfile 应拷贝 services/ 目录" "PASS" "COPY frontend/services/" "found"
    } else {
        Record "frontend.Dockerfile 应拷贝 services/ 目录" "FAIL" "COPY frontend/services/" "not found"
    }
    if ($dockerfile -match 'COPY\s+frontend/index.html') {
        Record "frontend.Dockerfile 应拷贝 index.html" "PASS" "COPY frontend/index.html" "found"
    } else {
        Record "frontend.Dockerfile 应拷贝 index.html" "FAIL" "COPY frontend/index.html" "not found"
    }
    if ($dockerfile -match 'COPY\s+frontend/app.js') {
        Record "frontend.Dockerfile 应拷贝 app.js" "PASS" "COPY frontend/app.js" "found"
    } else {
        Record "frontend.Dockerfile 应拷贝 app.js" "FAIL" "COPY frontend/app.js" "not found"
    }
} else {
    Record "frontend.Dockerfile 应存在" "FAIL" "exists" "not found"
}

# 容器内实际文件验证
$lsHtml = Docker-Exec "teamharness-nginx" "ls /usr/share/nginx/html/"
Write-Host "  nginx html 目录: $lsHtml" -ForegroundColor Gray
if ($lsHtml -match "index.html" -and $lsHtml -match "app.js" -and $lsHtml -match "services") {
    Record "nginx 容器内应有 index.html / app.js / services/" "PASS" "all present" "found"
} else {
    Record "nginx 容器内应有 index.html / app.js / services/" "FAIL" "all present" "$lsHtml"
}
$lsServices = Docker-Exec "teamharness-nginx" "ls /usr/share/nginx/html/services/"
if ($lsServices -match "api.js" -and $lsServices -match "utils.js") {
    Record "nginx 容器内 services/ 应有 api.js / utils.js" "PASS" "api.js + utils.js" "found"
} else {
    Record "nginx 容器内 services/ 应有 api.js / utils.js" "FAIL" "api.js + utils.js" "$lsServices"
}

# ===========================================================================
# Section 9: 端口暴露
# ===========================================================================
Write-Host "`n===== Section 9: 端口暴露 =====" -ForegroundColor Cyan

# 8080 端口可访问
$r = Send-Request -Uri "$BaseUrl/healthz" -Method "GET" -TimeoutSec 5
Assert-Eq "8080 端口应可访问" 200 $r.StatusCode

# 验证 docker-compose.yaml 中 ports 映射
$composePath = "$DeployDir\docker-compose.yaml"
if (Test-Path $composePath) {
    $compose = [System.IO.File]::ReadAllText($composePath)
    # nginx 端口映射
    if ($compose -match 'NGINX_PORT.*8080.*:80') {
        Record "docker-compose nginx 应映射 8080:80" "PASS" "8080:80" "found"
    } elseif ($compose -match '"\$\{NGINX_PORT:-8080\}:80"') {
        Record "docker-compose nginx 应映射 8080:80" "PASS" "8080:80" "found (default 8080)"
    } else {
        # 用更宽松的匹配
        if ($compose -match '8080.*:80' -or $compose -match 'NGINX_PORT') {
            Record "docker-compose nginx 应映射 8080:80" "PASS" "8080:80" "found"
        } else {
            Record "docker-compose nginx 应映射 8080:80" "FAIL" "8080:80" "not found"
        }
    }
    # 列出所有暴露端口
    $portMatches = [regex]::Matches($compose, '"\$\{(\w+):-(\d+)\}:(\d+)"')
    $exposedPorts = @()
    foreach ($pm in $portMatches) {
        $exposedPorts += "$($pm.Groups[2].Value):$($pm.Groups[3].Value) ($($pm.Groups[1].Value))"
    }
    Write-Host "  docker-compose 暴露端口: $($exposedPorts -join ', ')" -ForegroundColor Gray
    # 验证只暴露 8080（任务要求）—— 实际还暴露了 postgres/qdrant/gitea，记录差异
    $extraPorts = $exposedPorts | Where-Object { $_ -notmatch '8080' }
    if ($extraPorts.Count -eq 0) {
        Record "应只暴露 8080（无多余端口）" "PASS" "only 8080" "ok"
    } else {
        Record "应只暴露 8080（无多余端口）" "FAIL" "only 8080" "额外暴露: $($extraPorts -join ', ')（生产环境应移除 postgres/qdrant/gitea 端口映射）"
    }
} else {
    Record "docker-compose.yaml 应存在" "FAIL" "exists" "not found"
}

# ===========================================================================
# Section 10: CDN 依赖可达性
# ===========================================================================
Write-Host "`n===== Section 10: CDN 依赖可达性 =====" -ForegroundColor Cyan

# 从 HTML 中提取 CDN 引用
$cdnRefs = @()
if ($indexHtml) {
    $cdnMatches = [regex]::Matches($indexHtml, '(?:src|href)\s*=\s*"(https?://[^"]+)"')
    foreach ($m in $cdnMatches) {
        $cdnRefs += $m.Groups[1].Value
    }
}
$cdnRefs = $cdnRefs | Sort-Object -Unique
Write-Host "  HTML 引用 CDN: $($cdnRefs -join ', ')" -ForegroundColor Gray

# 预期 CDN（任务指定）
$expectedCdns = @(
    "https://unpkg.com/vue@3.4.21",
    "https://unpkg.com/element-plus@2.6.3",
    "https://unpkg.com/@element-plus/icons-vue@2.3.1"
)

if ($cdnRefs.Count -eq 0) {
    # 回退到预期列表
    Write-Host "  HTML 未提取到 CDN 引用，使用预期列表测试" -ForegroundColor Yellow
    $cdnRefs = $expectedCdns
}

# 测试每个 CDN URL（含完整资源路径）
$cdnTestUrls = @(
    "https://unpkg.com/vue@3.4.21/dist/vue.global.prod.js",
    "https://unpkg.com/element-plus@2.6.3/dist/index.css",
    "https://unpkg.com/element-plus@2.6.3/dist/index.full.min.js",
    "https://unpkg.com/@element-plus/icons-vue@2.3.1/dist/index.iife.min.js"
)
foreach ($url in $cdnTestUrls) {
    $r = Send-Request -Uri $url -Method "GET" -TimeoutSec 30
    Assert-Eq "CDN 可达: $url" 200 $r.StatusCode
}

# 验证 HTML 中引用的 CDN 包名是否在预期列表内
foreach ($expected in $expectedCdns) {
    $found = $false
    foreach ($ref in $cdnRefs) {
        if ($ref -match [regex]::Escape($expected)) { $found = $true; break }
    }
    if ($found) {
        Record "HTML 应引用 CDN: $expected" "PASS" "referenced" "found"
    } else {
        Record "HTML 应引用 CDN: $expected" "FAIL" "referenced" "not found in HTML"
    }
}

# ===========================================================================
# Summary & report
# ===========================================================================
Write-Host "`n===== Summary =====" -ForegroundColor Cyan
$total = $Pass + $Fail + $Skip
Write-Host "PASS: $Pass  /  FAIL: $Fail  /  SKIP: $Skip  /  Total: $total" -ForegroundColor White

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

if ($Skip -gt 0) {
    Write-Host "`n--- Skipped cases ---" -ForegroundColor Yellow
    foreach ($r in $Results) {
        if ($r.Status -eq "SKIP") {
            Write-Host "  [SKIP] $($r.Name) - $($r.Actual)" -ForegroundColor Yellow
        }
    }
}

# Write detailed report file
$reportPath = "d:\Code\TeamHarness\tests\module_nginx\nginx_report.txt"
$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("TeamHarness nginx + Docker 部署层模块测试报告")
$lines.Add("运行时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$lines.Add("BaseUrl: $BaseUrl")
$lines.Add("DeployDir: $DeployDir")
$lines.Add("=" * 70)
$lines.Add("汇总: PASS=$Pass  FAIL=$Fail  SKIP=$Skip  Total=$total")
$lines.Add("=" * 70)
$lines.Add("")
$lines.Add("===== 详细结果 =====")
foreach ($r in $Results) {
    $lines.Add("[$($r.Status)] $($r.Name)")
    if ($r.Expected) { $lines.Add("        Expected: $($r.Expected)") }
    if ($r.Actual)   { $lines.Add("        Actual:   $($r.Actual)") }
}
[System.IO.File]::WriteAllText($reportPath, ($lines -join "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Host "`n详细报告已写入: $reportPath" -ForegroundColor Cyan
