<#
.SYNOPSIS
I-DO System One-Click Deployment Script
.DESCRIPTION
Automatically complete Docker image building, service startup, health checks, etc.
#>

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  I-DO - Local Docker Deployment" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check if Docker is running
Write-Host "[1/10] Checking Docker environment..." -ForegroundColor Yellow
try {
    docker info > $null 2>&1
    Write-Host "[OK] Docker is running" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Docker is not running, please start Docker Desktop first" -ForegroundColor Red
    exit 1
}

# 2. Check .env file
Write-Host ""
Write-Host "[2/10] Checking .env configuration..." -ForegroundColor Yellow
if (-Not (Test-Path ".env")) {
    Write-Host "[WARN] .env file not found, creating from template..." -ForegroundColor Yellow
    Copy-Item "api/.env.example" ".env"
    Write-Host "[OK] .env file created, please modify the configuration as needed" -ForegroundColor Green
} else {
    Write-Host "[OK] .env file exists" -ForegroundColor Green
}

# 3. Clean up old containers
Write-Host ""
Write-Host "[3/10] Cleaning up old containers and networks..." -ForegroundColor Yellow
docker compose down --remove-orphans 2>&1 | Out-Null
Write-Host "[OK] Old containers cleaned up" -ForegroundColor Green

# 4. Build Sandbox image
Write-Host ""
Write-Host "[4/10] Building Sandbox image..." -ForegroundColor Yellow
Write-Host "This includes: Chromium, Python 3.10, Node.js, VNC, Chinese fonts, etc." -ForegroundColor Gray

Push-Location sandbox
docker build -t sandbox:latest .
Pop-Location

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Sandbox image build failed, please check logs" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Sandbox image built successfully" -ForegroundColor Green

# 5. Build MCP Multimodal image
Write-Host ""
Write-Host "[5/10] Building MCP Multimodal image..." -ForegroundColor Yellow
Write-Host "This includes: Python 3.12, Playwright Chromium, MCP streamable-http server" -ForegroundColor Gray

Push-Location mcp-multimodal
docker build -t mcp-multimodal:latest .
Pop-Location

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] MCP Multimodal image build failed, please check logs" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] MCP Multimodal image built successfully" -ForegroundColor Green

# 6. Build API image
Write-Host ""
Write-Host "[6/10] Building API image..." -ForegroundColor Yellow

Push-Location api
docker build -t api:latest .
Pop-Location

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] API image build failed, please check logs" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] API image built successfully" -ForegroundColor Green

# 7. Start services
Write-Host ""
Write-Host "[7/10] Starting all services..." -ForegroundColor Yellow

docker compose up -d

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Service startup failed, please check logs" -ForegroundColor Red
    exit 1
}

Write-Host "[OK] Service startup command executed" -ForegroundColor Green

# 8. Wait and check service status
Write-Host ""
Write-Host "[8/10] Waiting for services to be ready (approx 30-60 seconds)..." -ForegroundColor Yellow
Start-Sleep -Seconds 30

Write-Host ""
Write-Host "Checking service status..." -ForegroundColor Yellow
docker compose ps

# 9. Verify MCP Multimodal health
Write-Host ""
Write-Host "[9/10] Verifying MCP Multimodal service..." -ForegroundColor Yellow
Start-Sleep -Seconds 10

try {
    $healthResp = Invoke-WebRequest -Uri "http://localhost:9100/health" -TimeoutSec 5 -ErrorAction Stop 2>$null
    if ($healthResp.StatusCode -eq 200) {
        Write-Host "[OK] MCP Multimodal service is healthy" -ForegroundColor Green
    } else {
        Write-Host "[WARN] MCP Multimodal service returned status: $($healthResp.StatusCode)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "[WARN] MCP Multimodal health check via localhost:9100 unavailable" -ForegroundColor Yellow
}

# 10. Verify SearXNG health
Write-Host ""
Write-Host "[10/10] Verifying SearXNG search engine..." -ForegroundColor Yellow

try {
    $searxResp = docker compose exec searxng wget -qO- http://127.0.0.1:8080/healthz 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] SearXNG search engine is healthy" -ForegroundColor Green
    } else {
        Write-Host "[WARN] SearXNG health check failed" -ForegroundColor Yellow
    }
} catch {
    Write-Host "[WARN] SearXNG health check unavailable" -ForegroundColor Yellow
    Write-Host "      Service is accessible within Docker network at searxng:8080" -ForegroundColor Gray
}

# 11. Display deployment summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Deployment Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Backend API:  http://localhost:8000" -ForegroundColor White
Write-Host "API Docs:     http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host "MCP Multimodal: http://mcp-multimodal:9100/mcp (Docker network)" -ForegroundColor White
Write-Host "SearXNG Search:  http://searxng:8080 (Docker network)" -ForegroundColor White
Write-Host ""
Write-Host "Frontend UI:  Start manually in ui/ folder:" -ForegroundColor White
Write-Host "  cd ui && pnpm install && pnpm dev" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Pre-installed Sandbox Dependencies" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Browser:       Chromium" -ForegroundColor White
Write-Host "Python:        3.10 (pandas, numpy, openpyxl, matplotlib, opencv, rembg, etc.)" -ForegroundColor White
Write-Host "Node.js:       24.x LTS" -ForegroundColor White
Write-Host "VNC:           Xvfb + x11vnc + websockify (port 5900)" -ForegroundColor White
Write-Host "CDP:           Chrome DevTools Protocol (port 9222)" -ForegroundColor White
Write-Host "Chinese:       fonts-noto-cjk, zh_CN.UTF-8 locale" -ForegroundColor White
Write-Host ""
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  MCP Multimodal Service" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host "Transport:     streamable_http (/mcp)" -ForegroundColor White
Write-Host "Tools:         vl_image_understand, webpage_visual_analyse, ocr_extract," -ForegroundColor White
Write-Host "               asr_speech2text, video_analyse, pdf_multimodal_parse," -ForegroundColor White
Write-Host "               ppt_multimodal_parse, image_create" -ForegroundColor White
Write-Host ""
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "  SearXNG Search Engine" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Yellow
Write-Host "Strategy:      SearXNG(primary) -> Bing(fallback)" -ForegroundColor White
Write-Host "Engines:       Google, Bing, DuckDuckGo, Wikipedia, Brave" -ForegroundColor White
Write-Host "Language:      zh-CN (default)" -ForegroundColor White
Write-Host ""
Write-Host "View logs:   docker compose logs -f" -ForegroundColor Gray
Write-Host "Stop:        docker compose down" -ForegroundColor Gray
Write-Host "Restart:     docker compose restart" -ForegroundColor Gray
Write-Host ""
